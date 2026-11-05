"""Shadow routing: decide, log, never act.

On session create, each backend named in ``OMNIGENT_ROUTER_SHADOW`` (comma
list, e.g. ``rules,smart``) produces an :class:`ExecutionDecision` that is
appended to ``<data dir>/routing/decisions.jsonl`` next to what the user
actually chose. The create response is never affected: the hook runs in a
background task with a timeout and swallows every error.

Extension point: further backends (a hosted router, a small LLM, a local
model) implement :class:`ShadowRouter` and are registered in
:func:`build_shadow_routers`. No network backend ships here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnigent.process_logging import data_dir
from omnigent.server.smart_routing import RoutingClient, infer_models

_logger = logging.getLogger(__name__)

SHADOW_ENV_VAR = "OMNIGENT_ROUTER_SHADOW"
RULES_FILE_ENV_VAR = "OMNIGENT_ROUTER_RULES"
SHADOW_TIMEOUT_S = 5.0
MAX_RECORD_BYTES = 16_384
_MAX_TEXT_CHARS = 500


@dataclass(frozen=True)
class ExecutionDecision:
    """A router's richer recommendation for how a session should run.

    Every field except ``rationale`` and ``backend`` is optional so a backend
    reports only what it actually decides.

    :param tier: Coarse cost tier hint (``"cheap"``, ``"standard"``,
        ``"premium"``) when the backend cannot name a concrete model.
    :param host_needs: Free-form requirements, e.g. ``["gpu"]``.
    :param executor: Name of a known executor profile.
    """

    rationale: str
    backend: str
    agent_id: str | None = None
    harness: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    role: str | None = None
    executor: str | None = None
    host_needs: tuple[str, ...] | None = None
    tier: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ActualChoice:
    """What the session was actually created with."""

    agent_id: str | None = None
    harness: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    host_id: str | None = None


# Choice dimension -> create-request fields that set it explicitly.
# ``agent_id`` is required on every create, so only an executor marks the agent as chosen.
_PIN_FIELDS: Mapping[str, tuple[str, ...]] = {
    "agent": ("executor",),
    "harness": ("harness_override",),
    "model": ("model_override",),
    "effort": ("reasoning_effort",),
    # Managed creates move ``host_id`` into ``requested_host_id``. ``host_needs`` is a
    # constraint the router must honour, not a choice, so it is not a pin.
    "host": ("host_id", "requested_host_id", "host_preference"),
}


def pinned_dimensions(body: Any) -> list[str]:  # type: ignore[explicit-any]  # schema objects
    """Choice dimensions the caller (or its executor profile) set explicitly."""
    return [
        dim
        for dim, fields in _PIN_FIELDS.items()
        if any(getattr(body, name, None) is not None for name in fields)
    ]


@dataclass(frozen=True)
class DecisionRequest:
    """Everything a shadow router may look at for one session create.

    :param message: First message text ("" when the create carried none).
    :param available_models: Harness to model ids, cheapest first. May be
        empty at create time, before any runner has reported its catalog.
    :param executor_profiles: Known executor profile names (names only).
    """

    message: str
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    site: str = "session_create"
    session_id: str | None = None
    project: str | None = None
    alias: str | None = None
    available_models: Mapping[str, Sequence[str]] = field(default_factory=dict)
    executor_profiles: Sequence[str] = ()
    actual: ActualChoice = field(default_factory=ActualChoice)
    pinned: Sequence[str] = ()


class ShadowRouter(Protocol):
    """A backend that proposes a decision without acting on it."""

    name: str

    async def decide(self, request: DecisionRequest) -> ExecutionDecision | None:
        """Return a decision, or ``None`` when the backend has no opinion."""
        ...


# ── Rules backend ──────────────────────────────────────────────────────────

DEFAULT_RULES: dict[str, Any] = {  # type: ignore[explicit-any]  # parsed YAML
    "rules": [
        {
            "name": "benchmark-work",
            "match": {"any_keywords": ["benchmark", "eval", "evaluate"]},
            "decision": {"role": "Bench", "rationale": "benchmark/eval keywords"},
        },
        {
            "name": "short-question",
            "match": {"max_chars": 160, "question": True},
            "decision": {"tier": "cheap", "rationale": "short question"},
        },
        {
            "name": "long-prompt",
            "match": {"min_chars": 4000},
            "decision": {"tier": "premium", "rationale": "long prompt"},
        },
    ]
}


def default_rules_path() -> Path:
    """Return the rules file path (``OMNIGENT_ROUTER_RULES`` or ``~/.omnigent``)."""
    override = os.environ.get(RULES_FILE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".omnigent" / "router_rules.yaml"


def load_rules(path: Path | None = None) -> list[dict[str, Any]]:  # type: ignore[explicit-any]  # parsed YAML
    """Load rule dicts from *path*, falling back to the built-in defaults.

    A missing or malformed file yields the defaults (malformed is logged).
    """
    rules_path = path or default_rules_path()
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        rules = raw.get("rules") if isinstance(raw, dict) else None
        if isinstance(rules, list):
            return [rule for rule in rules if isinstance(rule, dict)]
        _logger.warning("router rules %s has no 'rules' list; using defaults", rules_path)
    except FileNotFoundError:
        pass
    except (OSError, yaml.YAMLError):
        _logger.warning("router rules %s unreadable; using defaults", rules_path, exc_info=True)
    return list(DEFAULT_RULES["rules"])


_KNOWN_MATCH_KEYS = frozenset({"max_chars", "min_chars", "question", "any_keywords"})


def _rule_matches(match: Mapping[str, Any], message: str) -> bool:  # type: ignore[explicit-any]  # parsed YAML
    """True when every condition in *match* holds.

    An empty match, or one with no recognised key, never fires; unknown keys
    are ignored with a warning.
    """
    known = _KNOWN_MATCH_KEYS.intersection(match)
    unknown = set(match) - known
    if unknown:
        _logger.warning("router rule match has unknown keys %s; ignoring", sorted(unknown))
    if not known:
        return False
    text = message.strip()
    lowered = text.lower()
    if "max_chars" in match and len(text) > int(match["max_chars"]):
        return False
    if "min_chars" in match and len(text) < int(match["min_chars"]):
        return False
    if "question" in match and text.endswith("?") != bool(match["question"]):
        return False
    keywords = match.get("any_keywords")
    return not (
        keywords
        and not any(
            re.search(rf"\b{re.escape(str(word).lower())}\b", lowered) for word in keywords
        )
    )


def _pick_model(
    tier: str, available: Mapping[str, Sequence[str]], preferred_harness: str | None
) -> tuple[str | None, str | None]:
    """Map a tier to (harness, model) from cheapest-first lists; (None, None) if unknown."""
    harness = preferred_harness if preferred_harness in available else next(iter(available), None)
    models = list(available.get(harness, ())) if harness else []
    if not models:
        return None, None
    if tier == "cheap":
        return harness, models[0]
    if tier == "premium":
        return harness, models[-1]
    return harness, models[len(models) // 2]


class RulesShadowRouter:
    """Deterministic first-match-wins keyword/length rules."""

    name = "rules"

    def __init__(self, rules: Sequence[Mapping[str, Any]] | None = None) -> None:  # type: ignore[explicit-any]
        self._rules = list(rules) if rules is not None else load_rules()

    async def decide(self, request: DecisionRequest) -> ExecutionDecision | None:
        for rule in self._rules:
            if not _rule_matches(rule.get("match") or {}, request.message):
                continue
            spec = rule.get("decision") or {}
            tier = spec.get("tier")
            harness, model = spec.get("harness"), spec.get("model")
            if tier and model is None:
                harness, model = _pick_model(
                    tier, request.available_models, request.actual.harness
                )
            needs = spec.get("host_needs")
            return ExecutionDecision(
                rationale=f"rule {rule.get('name', '?')}: {spec.get('rationale', 'matched')}",
                backend=self.name,
                agent_id=spec.get("agent_id"),
                harness=harness,
                model=model,
                reasoning_effort=spec.get("reasoning_effort"),
                role=spec.get("role"),
                executor=spec.get("executor"),
                host_needs=tuple(needs) if needs else None,
                tier=tier,
                confidence=spec.get("confidence"),
            )
        return None


# ── Adapter over the existing Smart Routing client ─────────────────────────


class SmartRoutingShadowRouter:
    """Expose an existing :class:`RoutingClient` as a shadow backend."""

    name = "smart"

    def __init__(self, client: RoutingClient) -> None:
        self._client = client

    async def decide(self, request: DecisionRequest) -> ExecutionDecision | None:
        if not request.available_models:
            return None
        catalog = {harness: list(models) for harness, models in request.available_models.items()}
        result = await self._client.route(request.message, catalog)
        if result is None:
            return None
        return ExecutionDecision(
            rationale=result.rationale,
            backend=self.name,
            harness=result.harness,
            model=result.model,
        )


def build_shadow_routers(
    names: Sequence[str], routing_client: RoutingClient | None
) -> list[ShadowRouter]:
    """Instantiate the named backends; unknown or unavailable ones are skipped."""
    routers: list[ShadowRouter] = []
    for name in names:
        if name == "rules":
            routers.append(RulesShadowRouter())
        elif name == "smart" and routing_client is not None:
            routers.append(SmartRoutingShadowRouter(routing_client))
        else:
            _logger.warning("shadow router backend %r unknown or unavailable", name)
    return routers


# ── Decision log ───────────────────────────────────────────────────────────


def decisions_path() -> Path:
    """Return ``<data dir>/routing/decisions.jsonl`` (honours ``OMNIGENT_DATA_DIR``)."""
    return data_dir() / "routing" / "decisions.jsonl"


SCHEMA_VERSION = 1
# Kept even when a record is too large, so dropped decisions stay countable.
_IDENTITY_KEYS = (
    "schema_version",
    "ts",
    "decision_id",
    "site",
    "step_id",
    "session_id",
    "backend",
)
oversized_records = 0


def _encode_record(record: dict[str, Any]) -> str:  # type: ignore[explicit-any]  # JSON record
    """Serialize to one JSON line, shrinking free text until under the size cap."""
    line = json.dumps(record, default=str, separators=(",", ":"))
    if len(line.encode()) <= MAX_RECORD_BYTES:
        return line
    slim = dict(record)
    decision = dict(slim.get("decision") or {})
    decision["rationale"] = str(decision.get("rationale", ""))[:_MAX_TEXT_CHARS]
    slim["decision"] = decision
    slim["truncated"] = True
    line = json.dumps(slim, default=str, separators=(",", ":"))
    if len(line.encode()) <= MAX_RECORD_BYTES:
        return line
    global oversized_records
    oversized_records += 1
    _logger.warning("shadow routing record over %d bytes; keeping identity only", MAX_RECORD_BYTES)
    stub = {k: slim.get(k) for k in _IDENTITY_KEYS}
    stub["truncated"] = "dropped"
    return json.dumps(stub, separators=(",", ":"))


def append_jsonl_line(target: Path, line: str) -> None:
    """Append *line* plus a newline to *target* (``O_APPEND``, dir 0700, file 0600).

    A short write is finished; a failed one is closed with a newline so the next
    record never joins a fragment.
    """
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (line + "\n").encode()
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    written = 0
    try:
        # Tighten a pre-existing file and directory created with looser modes.
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
            os.chmod(target.parent, 0o700)
        try:
            while written < len(data):
                written += os.write(fd, data[written:])
        except OSError:
            if written:
                with contextlib.suppress(OSError):
                    os.write(fd, b"\n")
            raise
    finally:
        os.close(fd)


def append_decision(record: dict[str, Any], path: Path | None = None) -> None:  # type: ignore[explicit-any]  # JSON record
    """Append one record as a single line (one ``O_APPEND`` write)."""
    append_jsonl_line(path or decisions_path(), _encode_record(record))


def iter_jsonl_records(source: Path) -> Iterator[dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Yield dict records from a JSONL file, skipping blank, corrupt or non-UTF-8 lines."""
    try:
        handle = source.open("rb")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:  # includes UnicodeDecodeError
                continue
            if isinstance(record, dict):
                yield record


def iter_decisions(path: Path | None = None) -> Iterator[dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Yield records from the log, skipping blank or corrupt lines."""
    return iter_jsonl_records(path or decisions_path())


# ── Hook ───────────────────────────────────────────────────────────────────

# custom-lint: disable-next=workspace-scoped-cache -- holds task objects
_background_tasks: set[asyncio.Task[None]] = set()
_EXPECTED_MAX = 4096
_expected_lock = threading.Lock()
# custom-lint: disable-next=workspace-scoped-cache -- bounded, keyed by unique session id
_expected_decisions: OrderedDict[str, None] = OrderedDict()


def expect_decision(session_id: str) -> None:
    """Note that a decision for *session_id* is on its way to the log."""
    with _expected_lock:
        _expected_decisions[session_id] = None
        _expected_decisions.move_to_end(session_id)
        while len(_expected_decisions) > _EXPECTED_MAX:
            _expected_decisions.popitem(last=False)


def decision_expected(session_id: str) -> bool:
    """Whether this process routed *session_id*, so its decision can still land."""
    with _expected_lock:
        return session_id in _expected_decisions


def shadow_backend_names() -> list[str]:
    """Parse ``OMNIGENT_ROUTER_SHADOW`` into backend names (empty when unset)."""
    raw = os.environ.get(SHADOW_ENV_VAR, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


async def run_shadow_routing(
    request: DecisionRequest,
    routers: Sequence[ShadowRouter],
    *,
    project: str | None = None,
    path: Path | None = None,
    timeout_s: float = SHADOW_TIMEOUT_S,
) -> None:
    """Run each router and log its decision; never raises."""
    for router in routers:
        started = time.monotonic()
        try:
            decision = await asyncio.wait_for(router.decide(request), timeout=timeout_s)
            if decision is None:
                continue
            record = {
                "schema_version": SCHEMA_VERSION,
                "ts": time.time(),
                "decision_id": request.decision_id,
                "site": request.site,
                # Workflow step ids replace this once workflows exist.
                "step_id": request.session_id,
                "session_id": request.session_id,
                # Project id for now; the namespace path once projects nest.
                "namespace": project if project is not None else request.project,
                "role": request.alias,
                "pinned": list(request.pinned),
                "backend": router.name,
                "decision": asdict(decision),
                "actual": asdict(request.actual),
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
            await asyncio.to_thread(append_decision, record, path)
            if path is None:
                from omnigent.server.routing_outcomes import flush_pending_outcomes

                await asyncio.to_thread(flush_pending_outcomes)
        except Exception:
            _logger.warning("shadow router %r failed", getattr(router, "name", "?"), exc_info=True)


async def _build_and_run(
    request: DecisionRequest, names: Sequence[str], client: RoutingClient | None
) -> None:
    """Build routers off the event loop (rules file I/O), then run them; never raises."""
    try:
        routers = await asyncio.to_thread(build_shadow_routers, names, client)
        if routers:
            await run_shadow_routing(request, routers)
    except Exception:
        _logger.warning("shadow routing setup failed", exc_info=True)


def schedule_shadow_routing(
    request: DecisionRequest,
    routing_client_factory: Any = None,  # type: ignore[explicit-any]
) -> None:
    """Fire-and-forget shadow routing for a created session.

    No-op unless ``OMNIGENT_ROUTER_SHADOW`` names backends. Returns immediately;
    all work and errors happen in a background task.

    :param routing_client_factory: Optional zero-arg callable returning the
        deployment's :class:`RoutingClient` (used by the ``smart`` backend).
    """
    try:
        names = shadow_backend_names()
        if not names:
            return
        client = routing_client_factory() if routing_client_factory else None
        task = asyncio.get_running_loop().create_task(
            _build_and_run(request, names, client), name="shadow-routing"
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception:
        _logger.warning("shadow routing could not be scheduled", exc_info=True)


def first_message_text(initial_input: Any, initial_items: Sequence[Any] = ()) -> str:  # type: ignore[explicit-any]  # schema objects
    """Extract the first user text from a create request's startup input/items."""
    for event in ([initial_input] if initial_input is not None else []) + list(initial_items):
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if isinstance(content, str):
            return content
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
    return ""


def shadow_route_created_session(  # type: ignore[explicit-any]  # schema objects
    body: Any, resp: Any, harness: str | None = None
) -> None:
    """Schedule shadow routing for a just-created session; never raises.

    :param body: Create request (JSON body or parsed multipart metadata).
    :param resp: Created session (``id``) or bundled create result (``session_id``).
    :param harness: Harness override when *resp* does not carry one (bundled creates).
    """
    if not shadow_backend_names():
        return
    try:
        from omnigent.runtime import get_caps
        from omnigent.server.routing_backend import backends_from_caps

        actual_harness = harness or getattr(resp, "harness", None)
        family_models = infer_models(actual_harness)
        session_id = getattr(resp, "id", None) or getattr(resp, "session_id", None)
        if session_id:
            expect_decision(session_id)
        schedule_shadow_routing(
            DecisionRequest(
                message=first_message_text(
                    getattr(body, "initial_input", None), getattr(body, "initial_items", ())
                ),
                session_id=session_id,
                project=getattr(resp, "project_id", None) or getattr(body, "project_id", None),
                alias=getattr(body, "alias", None),
                actual=ActualChoice(
                    agent_id=getattr(resp, "agent_id", None),
                    harness=actual_harness,
                    model=getattr(resp, "model_override", None)
                    or getattr(resp, "llm_model", None),
                    reasoning_effort=getattr(resp, "reasoning_effort", None),
                    host_id=getattr(resp, "host_id", None) or getattr(body, "host_id", None),
                ),
                pinned=pinned_dimensions(body),
                available_models={actual_harness: family_models}
                if actual_harness and family_models
                else {},
            ),
            lambda: backends_from_caps(get_caps()).any(),
        )
    except Exception:
        _logger.warning("shadow routing hook failed", exc_info=True)

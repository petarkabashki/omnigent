"""Routing outcome stream: what happened after a routing decision.

Events are appended to ``<data dir>/routing/outcomes.jsonl`` beside
``decisions.jsonl`` (same 0600 ``O_APPEND`` path), keyed by ``decision_id``.
The decision log stays write-once; every later fact about the session lands
here. Emitters know only a session id, so the decision id is resolved from the
decision log (or a ``rotated`` line). Emitting never raises. An event that
lands before its decision is logged is buffered and written once the decision
appears; one that cannot be joined is counted in a ``dropped`` line.

Events (``event`` field), each with ``at`` (epoch seconds) and ``session_id``:

* ``override``: ``kind`` (interrupt, harness_change, model_change,
  host_change, reset, rerun), ``actor``. Only ``actor="owner"`` counts.
  ``actor="unknown"`` means a change seen with no way to tell who caused it,
  so the step can be neither a success nor an override (``success_excluded``).
* ``cost``: cumulative snapshot with ``usd_total`` (``total_cost_usd``),
  ``usd_policy``, ``credits_total``, ``cost_source``, ``native_cost_complete``
  (null off the native path) and ``session_usage``. Not written when the
  session has no total. ``cost_source`` says what the figure is, and none of
  its values means billed money: ``harness`` (the harness's or provider's own
  figure), ``catalog`` (tokens priced from a price list), ``credits`` (a
  non-dollar meter: ``usd_total`` is null and the amount is ``credits_total``),
  ``unknown`` (the reporter did not declare a source), ``mixed`` or ``none``.
* ``ended``: a ``cost`` snapshot plus ``reason`` (stop, delete) taken when the
  session stops or is deleted. ``usd_total`` is always present; null means no
  data. ``cost_source`` is null when this process never saw the session's spend.
* ``rotated``: ``from_session``, ``to_session``. A native ``/clear`` moved the
  terminal to a new session; its lines keep the original ``decision_id``.
* ``check_declared``: ``check_ref``, written before any result exists.
* ``check``: ``status`` (passed, failed, error), ``passed`` (null on error).
* ``accept``: the owner's explicit accept gesture.
* ``dropped``: ``decision_id`` null; an event that could not be joined.

``cost`` and ``ended`` are cumulative per session and may append out of order,
so readers take the max per session and sum across sessions
(:func:`summarize_outcomes`).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.server.shadow_routing import (
    SCHEMA_VERSION,
    append_jsonl_line,
    decisions_path,
    iter_jsonl_records,
    shadow_backend_names,
)

_logger = logging.getLogger(__name__)

ACTOR_UNKNOWN = "unknown"
OVERRIDE_KINDS = frozenset(
    {"interrupt", "harness_change", "model_change", "host_change", "reset", "rerun"}
)
CHECK_STATUSES = frozenset({"passed", "failed", "error"})
ENDED_REASONS = frozenset({"stop", "delete", "launch_failed"})
COST_SOURCES = frozenset({"harness", "catalog", "credits", "unknown", "mixed", "none"})
# What a native reporter may declare for ``cumulative_cost_usd``.
DECLARED_COST_SOURCES = frozenset({"harness", "catalog", "credits"})
# ``session_usage`` key marking a total metered in credits, not dollars.
COST_UNIT_KEY = "cost_unit"
COST_UNIT_CREDITS = "credits"
# Field names a reader depends on; every cost/ended line and summary uses these.
USD_TOTAL = "usd_total"
USD_POLICY = "usd_policy"
CREDITS_TOTAL = "credits_total"
COST_SOURCE = "cost_source"
NATIVE_COST_COMPLETE = "native_cost_complete"
# ``session_usage`` keys copied into a cost snapshot so a reader can compare the
# stream with the stored totals, including the base a native reset rebased on.
_SNAPSHOT_KEYS = (
    "total_cost_usd",
    "policy_cost_usd",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "context_reset_base",
    NATIVE_COST_COMPLETE,
)
# An event waits this long for its decision before it is counted as dropped.
_PENDING_TTL_S = 120.0
_PENDING_MAX = 2000
_SOURCE_SESSIONS_MAX = 4096
dropped_outcomes = 0


def outcomes_path() -> Path:
    """Return ``<data dir>/routing/outcomes.jsonl`` (beside the decision log)."""
    return decisions_path().with_name("outcomes.jsonl")


class _SessionIndex:
    """Session id to decision id, built by reading a JSONL log incrementally."""

    def __init__(
        self,
        extract: Callable[[dict[str, Any]], tuple[str, str] | None],  # type: ignore[explicit-any]  # JSON record
    ) -> None:
        self._extract = extract
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._offset = 0
        self._by_session: dict[str, str] = {}

    def lookup(self, session_id: str, path: Path) -> str | None:
        with self._lock:
            try:
                size = path.stat().st_size
            except OSError:
                return None
            if path != self._path or size < self._offset:
                self._path, self._offset, self._by_session = path, 0, {}
            if size > self._offset:
                self._read_new(path)
            return self._by_session.get(session_id)

    def _read_new(self, path: Path) -> None:
        with path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        # A trailing line without a newline is still being written.
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self._offset += len(complete)
        for line in complete.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            pair = self._extract(record) if isinstance(record, dict) else None
            if pair is not None:
                self._by_session.setdefault(*pair)


def _decision_pair(record: dict[str, Any]) -> tuple[str, str] | None:  # type: ignore[explicit-any]  # JSON record
    session_id, decision_id = record.get("session_id"), record.get("decision_id")
    if isinstance(session_id, str) and isinstance(decision_id, str):
        return session_id, decision_id
    return None


def _rotation_pair(record: dict[str, Any]) -> tuple[str, str] | None:  # type: ignore[explicit-any]  # JSON record
    to_session, decision_id = record.get("to_session"), record.get("decision_id")
    if record.get("event") == "rotated" and isinstance(to_session, str):
        return (to_session, decision_id) if isinstance(decision_id, str) else None
    return None


# custom-lint: disable-next=workspace-scoped-cache -- tails one append-only log
_decision_index = _SessionIndex(_decision_pair)
# custom-lint: disable-next=workspace-scoped-cache -- tails one append-only log
_rotation_index = _SessionIndex(_rotation_pair)


def decision_id_for_session(session_id: str, path: Path | None = None) -> str | None:
    """Return the decision id for *session_id*, or ``None``.

    A rotated-to session resolves to the original decision, not any decision
    its own creation logged.

    :param path: The decision log to read; the outcome log is its sibling.
    """
    decisions = path or decisions_path()
    return _rotation_index.lookup(
        session_id, decisions.with_name("outcomes.jsonl")
    ) or _decision_index.lookup(session_id, decisions)


def append_outcome(
    decision_id: str | None,
    event: str,
    fields: Mapping[str, Any],  # type: ignore[explicit-any]  # JSON fields
    *,
    session_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one outcome record for *decision_id* (``at`` defaults to now)."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "session_id": session_id,
        "event": event,
        "at": time.time(),
        **fields,
    }
    append_jsonl_line(
        path or outcomes_path(), json.dumps(record, default=str, separators=(",", ":"))
    )


# ── Events that arrive before their decision ───────────────────────────────


@dataclass
class _Pending:
    session_id: str
    event: str
    fields: dict[str, Any]  # type: ignore[explicit-any]  # JSON fields
    path: Path | None
    queued: float


_pending_lock = threading.Lock()
# custom-lint: disable-next=workspace-scoped-cache -- bounded, drained on decision
_pending: list[_Pending] = []


def _decision_for(item_session: str, path: Path | None) -> str | None:
    return decision_id_for_session(
        item_session, path.with_name("decisions.jsonl") if path else None
    )


def _drop(item: _Pending, reason: str) -> None:
    """Count an event that could not be joined, in memory and in the stream."""
    global dropped_outcomes
    dropped_outcomes += 1
    _logger.warning("routing outcome %r for %s dropped: %s", item.event, item.session_id, reason)
    try:
        append_outcome(
            None,
            "dropped",
            {"dropped_event": item.event, "reason": reason},
            session_id=item.session_id,
            path=item.path,
        )
    except Exception:
        _logger.warning("routing drop marker not written", exc_info=True)


def _write_pending(item: _Pending, decision_id: str) -> None:
    append_outcome(
        decision_id, item.event, item.fields, session_id=item.session_id, path=item.path
    )


def flush_pending_outcomes() -> None:
    """Write buffered events whose decision is now logged; drop the expired. Never raises."""
    try:
        with _pending_lock:
            waiting, _pending[:] = list(_pending), []
        now, keep = time.monotonic(), []
        for item in waiting:
            decision_id = _decision_for(item.session_id, item.path)
            if decision_id is not None:
                _write_pending(item, decision_id)
            elif now - item.queued > _PENDING_TTL_S:
                _drop(item, "no_decision")
            else:
                keep.append(item)
        with _pending_lock:
            _pending[:0] = keep
    except Exception:
        _logger.warning("routing outcome flush failed", exc_info=True)


def _buffer(item: _Pending) -> None:
    with _pending_lock:
        overflow = len(_pending) >= _PENDING_MAX
        if not overflow:
            _pending.append(item)
    if overflow:
        _drop(item, "buffer_full")


def record_session_outcome(
    session_id: str,
    event: str,
    fields: Mapping[str, Any] | None = None,  # type: ignore[explicit-any]  # JSON fields
    *,
    path: Path | None = None,
) -> bool:
    """Append *event* for the decision logged for *session_id*; never raises.

    With no decision yet the event is buffered (when shadow routing is on, or
    *path* is given) and written once the decision is logged.

    :returns: ``True`` when a record was written now, ``False`` when it was
        buffered, the session has no decision, or the write failed.
    """
    try:
        flush_pending_outcomes()
        decision_id = _decision_for(session_id, path)
        item = _Pending(session_id, event, dict(fields or {}), path, time.monotonic())
        if decision_id is not None:
            _write_pending(item, decision_id)
            return True
        if path is not None or shadow_backend_names():
            item.fields.setdefault("at", time.time())
            _buffer(item)
        return False
    except Exception:
        _logger.warning("routing outcome %r not recorded", event, exc_info=True)
        return False


def record_override(session_id: str, kind: str, actor: str, *, path: Path | None = None) -> bool:
    """Record an override of the router's choice. Only ``actor="owner"`` counts."""
    if kind not in OVERRIDE_KINDS:
        raise ValueError(f"unknown override kind {kind!r}")
    return record_session_outcome(
        session_id, "override", {"kind": kind, "actor": actor}, path=path
    )


def record_rotation(from_session: str, to_session: str, *, path: Path | None = None) -> bool:
    """Record that a native ``/clear`` moved the terminal to *to_session*.

    Later lines on *to_session* join the original decision. Repeats are no-ops.
    """
    try:
        decisions = path.with_name("decisions.jsonl") if path else None
        known = decision_id_for_session(to_session, decisions)
        if known is not None and known == decision_id_for_session(from_session, decisions):
            return False
    except Exception:
        _logger.warning("routing rotation lookup failed", exc_info=True)
    return record_session_outcome(
        from_session,
        "rotated",
        {"from_session": from_session, "to_session": to_session},
        path=path,
    )


# ── Cost ───────────────────────────────────────────────────────────────────

# custom-lint: disable-next=workspace-scoped-cache -- bounded LRU of sources seen
_sources_seen: OrderedDict[str, set[str]] = OrderedDict()
_sources_lock = threading.Lock()


def _note_source(session_id: str, source: str | None) -> set[str]:
    """Remember what this report's spend was for the session; return all seen."""
    with _sources_lock:
        seen = _sources_seen.setdefault(session_id, set())
        _sources_seen.move_to_end(session_id)
        if source is not None and source in COST_SOURCES:
            seen.add(source)
        while len(_sources_seen) > _SOURCE_SESSIONS_MAX:
            _sources_seen.popitem(last=False)
        return set(seen)


def _combine_sources(seen: set[str]) -> str:
    return "mixed" if len(seen) > 1 else next(iter(seen), "none")


def _total(usage: Mapping[str, Any] | None) -> float | None:  # type: ignore[explicit-any]  # JSON fields
    value = (usage or {}).get("total_cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _is_credits(usage: Mapping[str, Any] | None) -> bool:  # type: ignore[explicit-any]  # JSON fields
    return (usage or {}).get(COST_UNIT_KEY) == COST_UNIT_CREDITS


def _native_complete(usage: Mapping[str, Any], native: bool | None) -> bool | None:  # type: ignore[explicit-any]  # JSON fields
    """The reporter's own completeness flag; null when it sent none or off the native path."""
    value = usage.get(NATIVE_COST_COMPLETE)
    return value if native and isinstance(value, bool) else None


def _usage_fields(
    usage: Mapping[str, Any] | None,  # type: ignore[explicit-any]  # JSON fields
    *,
    cost_source: str | None,
    native: bool | None,
) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON fields
    """Cost-snapshot fields from a ``session_usage`` mapping."""
    usage = usage or {}
    credits = _is_credits(usage)
    # A credit total is stored in the dollar key; it must not leave as dollars.
    hidden = ("total_cost_usd", "policy_cost_usd") if credits else ()
    return {
        USD_TOTAL: None if credits else _total(usage),
        USD_POLICY: None if credits else usage.get("policy_cost_usd"),
        CREDITS_TOTAL: _total(usage) if credits else None,
        COST_SOURCE: COST_UNIT_CREDITS if credits else cost_source,
        NATIVE_COST_COMPLETE: _native_complete(usage, native),
        "session_usage": {
            key: usage[key] for key in _SNAPSHOT_KEYS if key in usage and key not in hidden
        },
    }


def record_cost(
    session_id: str,
    usage: Mapping[str, Any] | None,  # type: ignore[explicit-any]  # JSON fields
    *,
    source: str | None,
    native: bool = False,
    context_epoch: int | None = None,
    path: Path | None = None,
) -> bool:
    """Record a cumulative cost snapshot of *usage* (the stored ``session_usage``).

    Writes nothing when *usage* has no total.

    :param source: What this report's cost is: ``"harness"`` (reported by the
        harness or provider), ``"catalog"`` (priced from tokens), ``"credits"``
        (a non-dollar meter), ``"unknown"`` (undeclared), or ``None`` when this
        report added no cost.
    :param native: Whether *usage* came from a native-harness cumulative report.
    """
    if _total(usage) is None:
        return False
    combined = _combine_sources(_note_source(session_id, source))
    fields = _usage_fields(usage, cost_source=combined, native=native)
    if context_epoch is not None:
        fields["context_epoch"] = context_epoch
    return record_session_outcome(session_id, "cost", fields, path=path)


def record_ended(
    session_id: str,
    usage: Mapping[str, Any] | None,  # type: ignore[explicit-any]  # JSON fields
    *,
    reason: str,
    native: bool = False,
    path: Path | None = None,
) -> bool:
    """Record that the session stopped or was deleted, with its final cost snapshot.

    The snapshot fields are always present: ``usd_total`` is null when the
    session was never priced. ``ended`` is not terminal; later spend raises the total.
    """
    if reason not in ENDED_REASONS:
        raise ValueError(f"unknown ended reason {reason!r}")
    with _sources_lock:
        seen = set(_sources_seen.get(session_id, ()))
    if _total(usage) is None:
        source = "none"
    else:
        source = _combine_sources(seen) if seen else None
    fields = {**_usage_fields(usage, cost_source=source, native=native), "reason": reason}
    return record_session_outcome(session_id, "ended", fields, path=path)


def record_accept(session_id: str, actor: str, *, path: Path | None = None) -> bool:
    """Record the explicit accept gesture (live success only; never replayable)."""
    return record_session_outcome(session_id, "accept", {"actor": actor}, path=path)


def record_check_declared(decision_id: str, check_ref: str, *, path: Path | None = None) -> bool:
    """Record that a check was declared, before any result exists; never raises."""
    try:
        append_outcome(decision_id, "check_declared", {"check_ref": check_ref}, path=path)
        return True
    except Exception:
        _logger.warning("routing check_declared not recorded", exc_info=True)
        return False


def record_check(
    decision_id: str,
    status: str,
    check_ref: str,
    *,
    path: Path | None = None,
) -> bool:
    """Record a check result; never raises. ``passed`` is null for ``error``.

    :returns: ``False`` for an unknown *status* or a failed write.
    """
    try:
        if status not in CHECK_STATUSES:
            raise ValueError(f"unknown check status {status!r}")
        passed = None if status == "error" else status == "passed"
        append_outcome(
            decision_id,
            "check",
            {"status": status, "passed": passed, "check_ref": check_ref},
            path=path,
        )
        return True
    except Exception:
        _logger.warning("routing check not recorded", exc_info=True)
        return False


# ── Readers ────────────────────────────────────────────────────────────────


def iter_outcomes(path: Path | None = None) -> Iterator[dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Yield outcome records, skipping blank or corrupt lines."""
    return iter_jsonl_records(path or outcomes_path())


def count_dropped(path: Path | None = None) -> dict[str, int]:
    """Count ``dropped`` lines by reason (events that could not be joined)."""
    counts: dict[str, int] = {}
    for record in iter_outcomes(path):
        if record.get("event") == "dropped":
            reason = str(record.get("reason"))
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _max_number(current: float | None, value: Any) -> float | None:  # type: ignore[explicit-any]  # JSON field
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return current
    return value if current is None else max(current, value)


def _sum_known(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _combine_session_sources(sources: set[str]) -> str:
    """Reduce per-line ``cost_source`` values; any disagreement is ``mixed``."""
    return (
        "mixed"
        if "mixed" in sources or len(sources - {"none"}) > 1
        else (next(iter(sources - {"none"}), "none"))
    )


def summarize_outcomes(path: Path | None = None) -> dict[str, dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Reduce the stream to one summary per ``decision_id``.

    Cumulative fields take the max per session, then sum across the sessions a
    decision spans (a ``/clear`` rotation adds one). ``sessions`` holds the
    per-session figures. ``usd_total`` is null when no session was priced.
    ``native_cost_complete`` is false when any session was incomplete, true
    when every session reported complete, and null otherwise: null means
    unknown or not applicable. ``check`` and ``check_declared`` keep
    the latest by ``at``. ``overrides`` lists every override record.
    ``success_excluded`` is true when an override's actor is unknown and no
    owner override settles the step: leave it out of success scoring.
    """
    summaries: dict[str, dict[str, Any]] = {}  # type: ignore[explicit-any]
    per_session: dict[str, dict[str | None, dict[str, Any]]] = {}  # type: ignore[explicit-any]
    for record in iter_outcomes(path):
        decision_id = record.get("decision_id")
        if not isinstance(decision_id, str):
            continue
        summary = summaries.setdefault(
            decision_id,
            {
                "session_id": None,
                "sessions": {},
                USD_TOTAL: None,
                USD_POLICY: None,
                CREDITS_TOTAL: None,
                COST_SOURCE: "none",
                NATIVE_COST_COMPLETE: None,
                "ended_at": None,
                "ended_reason": None,
                "accept_at": None,
                "check_declared": None,
                "check": None,
                "overrides": [],
                "success_excluded": False,
                "rotations": [],
            },
        )
        session_id = record.get("session_id")
        summary["session_id"] = summary["session_id"] or session_id
        event, at = record.get("event"), record.get("at")
        if event in ("cost", "ended"):
            entry = per_session.setdefault(decision_id, {}).setdefault(
                session_id,
                {
                    USD_TOTAL: None,
                    USD_POLICY: None,
                    CREDITS_TOTAL: None,
                    "sources": set(),
                    "native": [],
                },
            )
            entry[USD_TOTAL] = _max_number(entry[USD_TOTAL], record.get(USD_TOTAL))
            entry[USD_POLICY] = _max_number(entry[USD_POLICY], record.get(USD_POLICY))
            entry[CREDITS_TOTAL] = _max_number(entry[CREDITS_TOTAL], record.get(CREDITS_TOTAL))
            if isinstance(record.get(COST_SOURCE), str):
                entry["sources"].add(record[COST_SOURCE])
            if isinstance(record.get(NATIVE_COST_COMPLETE), bool):
                entry["native"].append(record[NATIVE_COST_COMPLETE])
            if event == "ended":
                if (summary["ended_at"] or 0) <= (at or 0):
                    summary["ended_reason"] = record.get("reason")
                summary["ended_at"] = _max_number(summary["ended_at"], at)
        elif event == "accept":
            summary["accept_at"] = _max_number(summary["accept_at"], at)
        elif event == "override":
            summary["overrides"].append(record)
        elif event == "rotated":
            summary["rotations"].append(record)
        elif event in ("check", "check_declared"):
            latest = summary[event]
            if latest is None or (at or 0) >= (latest.get("at") or 0):
                summary[event] = record
    for decision_id, sessions in per_session.items():
        summary = summaries[decision_id]
        sources: set[str] = set()
        native: list[bool | None] = []
        for session_id, entry in sessions.items():
            # A session whose last native report was incomplete stays incomplete.
            session_native = all(entry["native"]) if entry["native"] else None
            summary["sessions"][session_id] = {
                USD_TOTAL: entry[USD_TOTAL],
                USD_POLICY: entry[USD_POLICY],
                CREDITS_TOTAL: entry[CREDITS_TOTAL],
                COST_SOURCE: _combine_session_sources(entry["sources"]),
                NATIVE_COST_COMPLETE: session_native,
            }
            sources |= entry["sources"]
            native.append(session_native)
        summary[USD_TOTAL] = _sum_known([entry[USD_TOTAL] for entry in sessions.values()])
        summary[USD_POLICY] = _sum_known([entry[USD_POLICY] for entry in sessions.values()])
        summary[CREDITS_TOTAL] = _sum_known([entry[CREDITS_TOTAL] for entry in sessions.values()])
        summary[COST_SOURCE] = _combine_session_sources(sources)
        if False in native:
            summary[NATIVE_COST_COMPLETE] = False
        else:
            summary[NATIVE_COST_COMPLETE] = None if None in native else True
    for summary in summaries.values():
        actors = {override.get("actor") for override in summary["overrides"]}
        summary["success_excluded"] = ACTOR_UNKNOWN in actors and "owner" not in actors
    return summaries


def usage_gap(
    summary: Mapping[str, Any],  # type: ignore[explicit-any]  # JSON record
    session_usage: Mapping[str, Any],  # type: ignore[explicit-any]  # JSON record
    *,
    session_id: str | None = None,
) -> dict[str, float | None]:
    """Compare one :func:`summarize_outcomes` entry with the stored ``session_usage``.

    Each value is ``stream - stored`` in USD (negative means the stream
    under-counts), or ``None`` when either side is unpriced. Pass *session_id*
    to compare one session of a rotated decision; the default compares the sum.
    """
    streamed = summary["sessions"][session_id] if session_id is not None else summary

    def gap(streamed_value: Any, stored: Any) -> float | None:  # type: ignore[explicit-any]
        if streamed_value is None or stored is None:
            return None
        return float(streamed_value) - float(stored)

    return {
        USD_TOTAL: gap(streamed.get(USD_TOTAL), session_usage.get("total_cost_usd")),
        USD_POLICY: gap(streamed.get(USD_POLICY), session_usage.get("policy_cost_usd")),
    }

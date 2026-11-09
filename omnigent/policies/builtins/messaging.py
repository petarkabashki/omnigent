"""Built-in policy: who may message whom between independent sessions.

Rules live in a YAML file that the owner edits, keyed by role handles
(``project/Role``) rather than session ids, so new sessions taking a role need
no edit. The file is re-read whenever its modification time changes, so a rule
change takes effect on the next message without restarting the server::

    default: ask          # allow | ask | deny, for pairs no rule matches
    rules:
      - from: forthkit/Product
        to: touchkey/*
        action: allow
      - from: "*"
        to: "*/Coordinator"
        action: allow

Patterns are shell-style globs. The first matching rule decides. A session
with no role is ``project/`` (``/`` when unfiled), so ``project/*`` covers it.

``{project}`` in a ``from`` or ``to`` pattern stands for the sender's project,
so ``from: "*"`` with ``to: "{project}/*"`` covers messages within one project.

A rule may carry ``until:`` (ISO 8601 with a timezone, e.g.
``2026-11-09T14:00:00+00:00``); once that time has passed the rule is skipped.
An ``until`` without a timezone, or one that is not a timestamp, makes the file
unusable, so the last good parse keeps applying.

The server registers a :data:`HandleResolver` at startup because the event
carries only the target as typed; outside the server the policy asks.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import yaml

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_log = logging.getLogger(__name__)

MessagingAction = Literal["ALLOW", "ASK", "DENY"]

#: Tools that address another independent session. ``sys_session_send`` and
#: ``sys_session_create`` only reach the caller's own children, so they are
#: not covered.
MESSAGING_TOOLS = frozenset({"sys_session_message"})

_ACTIONS: dict[str, MessagingAction] = {"allow": "ALLOW", "ask": "ASK", "deny": "DENY"}


@dataclass(frozen=True)
class MessageRoute:
    """Resolved sender and target of one message, as role handles."""

    sender: str
    target: str


#: ``(sender_session_id, target_address, user_id) -> route``, or ``None`` when
#: the target does not resolve, which the delivery path then reports itself.
HandleResolver = Callable[[str, str, str | None], MessageRoute | None]

_resolver: HandleResolver | None = None


def set_handle_resolver(resolver: HandleResolver | None) -> None:
    """Install the server's handle resolver; ``None`` removes it."""
    global _resolver
    _resolver = resolver


def _glob_escape(text: str) -> str:
    """Escape glob metacharacters so *text* matches itself literally."""
    return re.sub(r"([*?\[])", r"[\1]", text)


@dataclass(frozen=True)
class MessagingRule:
    """One ``from`` / ``to`` / ``action`` line of the rules file."""

    sender: str
    target: str
    action: MessagingAction
    until: datetime | None = None

    def expired(self, now: datetime) -> bool:
        return self.until is not None and now >= self.until

    def matches(self, route: MessageRoute) -> bool:
        project = _glob_escape(route.sender.split("/", 1)[0])
        return fnmatch.fnmatchcase(
            route.sender, self.sender.replace("{project}", project)
        ) and fnmatch.fnmatchcase(route.target, self.target.replace("{project}", project))


@dataclass(frozen=True)
class MessagingDecision:
    """Outcome of :meth:`MessagingRules.match`; ``rule_index`` is 1-based, ``None`` if default."""

    action: MessagingAction
    reason: str
    rule_index: int | None


@dataclass(frozen=True)
class MessagingRules:
    """Parsed rules file."""

    default: MessagingAction
    rules: tuple[MessagingRule, ...]

    def match(self, route: MessageRoute, now: datetime | None = None) -> MessagingDecision:
        """Return the decision for *route*, skipping rules expired at *now*."""
        now = now or datetime.now(UTC)
        for index, rule in enumerate(self.rules, start=1):
            if not rule.expired(now) and rule.matches(route):
                return MessagingDecision(
                    rule.action,
                    f"messaging rule {index} ({rule.sender} -> {rule.target}: "
                    f"{rule.action.lower()}) for {route.sender} -> {route.target}",
                    index,
                )
        return MessagingDecision(
            self.default,
            f"no messaging rule for {route.sender} -> {route.target}; "
            f"default is {self.default.lower()}",
            None,
        )

    def decide(
        self, route: MessageRoute, now: datetime | None = None
    ) -> tuple[MessagingAction, str]:
        """Return the action for *route* and the reason shown with it."""
        decision = self.match(route, now)
        return decision.action, decision.reason


def _action(value: object, where: str) -> MessagingAction:
    action = _ACTIONS.get(str(value).strip().lower()) if value is not None else None
    if action is None:
        raise ValueError(f"{where}: action must be allow, ask or deny, got {value!r}")
    return action


def _until(value: object, where: str) -> datetime | None:
    """Parse an optional ``until`` into an aware datetime."""
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{where}: until must be an ISO 8601 timestamp, got {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{where}: until needs a timezone, e.g. +00:00, got {value!r}")
    return parsed


def parse_rules(text: str) -> MessagingRules:
    """Parse the YAML rules file; raises ``ValueError`` on a malformed file."""
    document = yaml.safe_load(text) or {}
    if not isinstance(document, dict):
        raise ValueError("rules file must be a mapping with 'default' and 'rules'")
    raw_rules = document.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ValueError("'rules' must be a list")
    rules: list[MessagingRule] = []
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"rule {index} must be a mapping")
        sender, target = raw.get("from"), raw.get("to")
        if not isinstance(sender, str) or not isinstance(target, str):
            raise ValueError(f"rule {index} needs string 'from' and 'to' patterns")
        where = f"rule {index}"
        rules.append(
            MessagingRule(
                sender, target, _action(raw.get("action"), where), _until(raw.get("until"), where)
            )
        )
    return MessagingRules(_action(document.get("default", "ask"), "default"), tuple(rules))


_RULES_KEY = re.compile(r"^rules:\s*(\[\s*\])?\s*(#.*)?$")
_TOP_LEVEL_KEY = re.compile(r"^[^\s#-]")


def insert_rule_text(
    text: str,
    sender: str,
    target: str,
    action: Literal["allow", "ask", "deny"],
    until: datetime | None = None,
) -> str:
    """Return *text* with a rule inserted first under ``rules:``, comments kept.

    :raises ValueError: if ``rules:`` is written in a form that cannot take an
        inserted item, or the result would not parse.
    """
    lines = text.splitlines(keepends=True)
    key_index = next((i for i, line in enumerate(lines) if _RULES_KEY.match(line)), None)
    indent = "  "
    if key_index is not None:
        for line in lines[key_index + 1 :]:
            if _TOP_LEVEL_KEY.match(line):
                break
            bullet = re.match(r"^(\s*)- ", line)
            if bullet:
                indent = bullet.group(1)
                break
    entry = [
        f"{indent}- from: {json.dumps(sender)}\n",
        f"{indent}  to: {json.dumps(target)}\n",
        f"{indent}  action: {action}\n",
    ]
    if until is not None:
        entry.append(f"{indent}  until: {json.dumps(until.isoformat())}\n")
    if key_index is None:
        if any(line.startswith("rules:") for line in lines):
            raise ValueError("cannot insert into 'rules:' written in flow style")
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["rules:\n", *entry]
    else:
        if "[" in lines[key_index]:
            comment = lines[key_index].partition("#")[2].strip()
            lines[key_index] = "rules:" + (f"  # {comment}" if comment else "") + "\n"
        lines[key_index + 1 : key_index + 1] = entry
    result = "".join(lines)
    parse_rules(result)
    return result


def write_text_atomically(path: str, text: str) -> None:
    """Replace *path* with *text* via a temp file and rename, keeping its mode."""
    path = os.path.expanduser(path)
    directory = os.path.dirname(os.path.abspath(path))
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".messaging-rules-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if os.path.exists(path):
            os.chmod(temp_path, os.stat(path).st_mode & 0o777)
        os.replace(temp_path, path)
    except BaseException:
        os.unlink(temp_path)
        raise


_write_lock = threading.Lock()


def add_allow_rule(path: str, sender: str, target: str, *, hours: float | None = None) -> None:
    """Insert an allow rule for *sender* -> *target* at the top of the rules file.

    :param hours: Lifetime of the rule; ``None`` makes it permanent.
    """
    until = datetime.now(UTC) + timedelta(hours=hours) if hours is not None else None
    path = os.path.expanduser(path)
    with _write_lock:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = ""
        write_text_atomically(path, insert_rule_text(text, sender, target, "allow", until))


class _RulesFile:
    """The rules file, re-parsed when its mtime changes; keeps the last good parse."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._rules: MessagingRules | None = None
        self._error: str | None = None

    def load(self) -> tuple[MessagingRules | None, str | None]:
        with self._lock:
            try:
                mtime = os.stat(self._path).st_mtime
                if mtime != self._mtime:
                    with open(self._path, encoding="utf-8") as handle:
                        self._rules = parse_rules(handle.read())
                    self._mtime, self._error = mtime, None
            except (OSError, ValueError, yaml.YAMLError) as exc:
                if self._error != str(exc):
                    _log.warning("messaging rules %s unusable: %s", self._path, exc)
                self._error = str(exc)
            return self._rules, self._error


def _user_id(event: PolicyEvent) -> str | None:
    actor = event.get("context", {}).get("actor") or {}
    run_as = actor.get("run_as") if isinstance(actor, dict) else None
    return run_as if isinstance(run_as, str) and run_as else None


def messaging_rules(*, path: str) -> PolicyCallable:
    """Factory: decide ``sys_session_message`` calls from a rules file.

    Pairs no rule matches get the file's ``default``. An unreadable file keeps
    the last good rules; before any good read, and outside the server, every
    message asks.

    :param path: Rules file path, ``~`` expanded, e.g.
        ``"~/work/omnigent/.omnigent/messaging-policy.yaml"``.
    :returns: A policy callable.
    """
    rules_file = _RulesFile(path)

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if data.get("name") not in MESSAGING_TOOLS:
            return None
        arguments = data.get("arguments")
        address = arguments.get("session_id") if isinstance(arguments, dict) else None
        sender_id = event.get("context", {}).get("conversation_id")
        if not isinstance(address, str) or not isinstance(sender_id, str):
            return None
        if _resolver is None:
            return {"result": "ASK", "reason": "Messaging rules can't resolve roles here."}
        route = _resolver(sender_id, address, _user_id(event))
        if route is None:
            return None
        rules, error = rules_file.load()
        if rules is None:
            return {"result": "ASK", "reason": f"Messaging rules file unusable: {error}"}
        action, reason = rules.decide(route)
        return cast(PolicyResponse, {"result": action, "reason": reason})

    return evaluate


POLICY_REGISTRY: list[dict[str, object]] = [
    {
        "handler": "omnigent.policies.builtins.messaging.messaging_rules",
        "kind": "factory",
        "name": "Messaging Rules",
        "description": (
            "Allow, ask or deny sys_session_message between sessions by role handle "
            "(project/Role), from a YAML rules file re-read whenever it changes."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the YAML rules file; ~ is expanded.",
                }
            },
            "required": ["path"],
        },
    }
]

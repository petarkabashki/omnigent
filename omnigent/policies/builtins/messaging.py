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

The server registers a :data:`HandleResolver` at startup because the event
carries only the target as typed; outside the server the policy asks.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class MessagingRule:
    """One ``from`` / ``to`` / ``action`` line of the rules file."""

    sender: str
    target: str
    action: MessagingAction

    def matches(self, route: MessageRoute) -> bool:
        return fnmatch.fnmatchcase(route.sender, self.sender) and fnmatch.fnmatchcase(
            route.target, self.target
        )


@dataclass(frozen=True)
class MessagingRules:
    """Parsed rules file."""

    default: MessagingAction
    rules: tuple[MessagingRule, ...]

    def decide(self, route: MessageRoute) -> tuple[MessagingAction, str]:
        """Return the action for *route* and the reason shown with it."""
        for index, rule in enumerate(self.rules, start=1):
            if rule.matches(route):
                return rule.action, (
                    f"messaging rule {index} ({rule.sender} -> {rule.target}: "
                    f"{rule.action.lower()}) for {route.sender} -> {route.target}"
                )
        return self.default, (
            f"no messaging rule for {route.sender} -> {route.target}; "
            f"default is {self.default.lower()}"
        )


def _action(value: object, where: str) -> MessagingAction:
    action = _ACTIONS.get(str(value).strip().lower()) if value is not None else None
    if action is None:
        raise ValueError(f"{where}: action must be allow, ask or deny, got {value!r}")
    return action


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
        rules.append(MessagingRule(sender, target, _action(raw.get("action"), f"rule {index}")))
    return MessagingRules(_action(document.get("default", "ask"), "default"), tuple(rules))


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

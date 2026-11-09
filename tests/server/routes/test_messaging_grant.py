"""Approving a messaging ASK can write an allow rule; only the verdict path does."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.policies.builtins.messaging import (
    MessageRoute,
    MessagingGrantOffer,
    parse_rules,
    set_handle_resolver,
)
from omnigent.server.routes._sessions import helpers
from omnigent.server.routes._sessions.common import (
    _pending_policy_ask_writes,
    _PendingPolicyAskWrites,
)
from omnigent.server.routes.messaging_rules import messaging_grant_offer

_HANDLER = "omnigent.policies.builtins.messaging.messaging_rules"
_ELICITATION = "elicit_grant"


def _pending(path: Path, *, from_mcp: bool) -> _PendingPolicyAskWrites:
    return _PendingPolicyAskWrites(
        state_updates=None,
        set_labels=None,
        from_mcp=from_mcp,
        message_grant=MessagingGrantOffer(str(path), "a/Sender", "b/Target"),
    )


async def _verdict(action: str, content: dict | None) -> None:
    data: dict = {"elicitation_id": _ELICITATION, "action": action}
    if content is not None:
        data["content"] = content
    await helpers._apply_pending_policy_ask_writes("s", object(), object(), object(), data)  # type: ignore[arg-type]


@pytest.fixture()
def rules_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Agentless sessions still build an engine; these offers stash no writes for it.
    monkeypatch.setattr(helpers, "_load_policy_spec_for_session", lambda *_args: None)
    monkeypatch.setattr(
        helpers, "_build_policy_engine_from_spec", lambda *_args: SimpleNamespace()
    )
    path = tmp_path / "rules.yaml"
    path.write_text("# mine\ndefault: ask\nrules: []\n")
    yield path
    _pending_policy_ask_writes.pop(_ELICITATION, None)


@pytest.mark.parametrize("from_mcp", [False, True])
async def test_always_choice_writes_a_permanent_rule(rules_path: Path, from_mcp: bool) -> None:
    _pending_policy_ask_writes[_ELICITATION] = _pending(rules_path, from_mcp=from_mcp)
    await _verdict("accept", {"messaging_grant": "always"})
    rule = parse_rules(rules_path.read_text()).rules[0]
    assert (rule.sender, rule.target, rule.action, rule.until) == (
        "a/Sender",
        "b/Target",
        "ALLOW",
        None,
    )
    assert "# mine" in rules_path.read_text()


async def test_two_hour_choice_sets_until(rules_path: Path) -> None:
    _pending_policy_ask_writes[_ELICITATION] = _pending(rules_path, from_mcp=False)
    await _verdict("accept", {"messaging_grant": "2h"})
    until = parse_rules(rules_path.read_text()).rules[0].until
    assert until is not None
    assert timedelta(hours=1, minutes=59) < until - datetime.now(UTC) <= timedelta(hours=2)


async def test_mcp_entry_survives_but_cannot_grant_twice(rules_path: Path) -> None:
    _pending_policy_ask_writes[_ELICITATION] = _pending(rules_path, from_mcp=True)
    await _verdict("accept", {"messaging_grant": "always"})
    assert _pending_policy_ask_writes[_ELICITATION].message_grant is None
    await _verdict("accept", {"messaging_grant": "always"})
    assert len(parse_rules(rules_path.read_text()).rules) == 1


@pytest.mark.parametrize(
    ("action", "content"),
    [
        ("decline", {"messaging_grant": "always"}),
        ("accept", None),
        ("accept", {"messaging_grant": "forever"}),
        ("accept", {"remember": True}),
    ],
)
async def test_only_an_accepted_known_choice_writes(
    rules_path: Path, action: str, content: dict | None
) -> None:
    before = rules_path.read_text()
    _pending_policy_ask_writes[_ELICITATION] = _pending(rules_path, from_mcp=False)
    await _verdict(action, content)
    assert rules_path.read_text() == before


async def test_choice_without_a_server_held_offer_writes_nothing(rules_path: Path) -> None:
    before = rules_path.read_text()
    _pending_policy_ask_writes[_ELICITATION] = _PendingPolicyAskWrites(None, None)
    await _verdict("accept", {"messaging_grant": "always"})
    assert rules_path.read_text() == before


def _store(*, enabled: bool = True) -> SimpleNamespace:
    policy = SimpleNamespace(
        name="msg", handler=_HANDLER, enabled=enabled, factory_params={"path": "/x/rules.yaml"}
    )
    return SimpleNamespace(list_defaults=lambda: [policy])


def test_offer_requires_the_messaging_policy_to_have_asked() -> None:
    set_handle_resolver(lambda *_args: MessageRoute("a/S", "b/T"))
    try:
        args = {"session_id": "b/T"}
        offer = messaging_grant_offer(_store(), "s", "sys_session_message", args, ["msg"], "u")
        assert offer == MessagingGrantOffer("/x/rules.yaml", "a/S", "b/T")
        assert (
            messaging_grant_offer(_store(), "s", "sys_session_message", args, ["other"], "u")
            is None
        )
        assert messaging_grant_offer(_store(), "s", "sys_os_shell", args, ["msg"], "u") is None
        assert (
            messaging_grant_offer(
                _store(enabled=False), "s", "sys_session_message", args, ["msg"], "u"
            )
            is None
        )
        assert messaging_grant_offer(None, "s", "sys_session_message", args, ["msg"], "u") is None
    finally:
        set_handle_resolver(None)

"""Messaging rules decide session-to-session messages by role handle."""

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.policies.builtins.messaging import (
    MessageRoute,
    messaging_rules,
    parse_rules,
    set_handle_resolver,
)
from omnigent.server.session_aliases import messaging_route_resolver
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY, PROJECT_LABEL_KEY
from tests.policies.builtins.helpers import tool_call_event

SENDER_ID = "a" * 32
TARGET_ID = "b" * 32

RULES = """
default: ask
rules:
  - from: forthkit/Product
    to: touchkey/*
    action: allow
  - from: "*"
    to: "*/Coordinator"
    action: allow
  - from: "*"
    to: secret/*
    action: deny
"""


def _message(address: str, sender: str = SENDER_ID) -> dict:
    event = tool_call_event("sys_session_message", {"session_id": address, "message": "hi"})
    event["context"]["conversation_id"] = sender
    event["context"]["actor"] = {"run_as": "user"}
    return event


@pytest.fixture()
def routes() -> Iterator[dict[str, str]]:
    """Resolve ``address -> target handle`` from a dict, sender fixed at ``forthkit/Product``."""
    table: dict[str, str] = {}
    set_handle_resolver(
        lambda _sender, address, _user: (
            MessageRoute("forthkit/Product", table[address]) if address in table else None
        )
    )
    yield table
    set_handle_resolver(None)


def _policy(tmp_path: Path, text: str = RULES):
    path = tmp_path / "messaging-policy.yaml"
    path.write_text(text)
    return path, messaging_rules(path=str(path))


def test_first_matching_rule_decides_and_names_itself(tmp_path, routes) -> None:
    routes.update({"touchkey/Bench": "touchkey/Bench", "secret/X": "secret/X"})
    _, policy = _policy(tmp_path)
    allowed = policy(_message("touchkey/Bench"))
    assert allowed["result"] == "ALLOW"
    assert "rule 1" in allowed["reason"] and "touchkey/Bench" in allowed["reason"]
    assert policy(_message("secret/X"))["result"] == "DENY"


def test_unmatched_pair_gets_the_default(tmp_path, routes) -> None:
    routes["other/Product"] = "other/Product"
    _, policy = _policy(tmp_path)
    response = policy(_message("other/Product"))
    assert response["result"] == "ASK"
    assert "default is ask" in response["reason"]


def test_edit_takes_effect_without_rebuilding_and_bad_edit_keeps_last_good(
    tmp_path, routes
) -> None:
    routes["other/Product"] = "other/Product"
    path, policy = _policy(tmp_path)
    assert policy(_message("other/Product"))["result"] == "ASK"
    path.write_text("default: deny\nrules: []\n")
    os.utime(path, (1, 1))
    assert policy(_message("other/Product"))["result"] == "DENY"
    path.write_text("default: maybe\n")
    os.utime(path, (2, 2))
    assert policy(_message("other/Product"))["result"] == "DENY"


def test_missing_file_asks(tmp_path, routes) -> None:
    routes["touchkey/Bench"] = "touchkey/Bench"
    policy = messaging_rules(path=str(tmp_path / "absent.yaml"))
    response = policy(_message("touchkey/Bench"))
    assert response["result"] == "ASK" and "unusable" in response["reason"]


def test_abstains_on_other_tools_and_unresolved_targets(tmp_path, routes) -> None:
    _, policy = _policy(tmp_path)
    assert policy(tool_call_event("sys_session_send", {"session_id": "x"})) is None
    assert policy(_message("nobody/Here")) is None


def test_asks_without_a_resolver(tmp_path) -> None:
    _, policy = _policy(tmp_path)
    assert policy(_message("touchkey/Bench"))["result"] == "ASK"


@pytest.mark.parametrize(
    "text",
    [
        "- just a list",
        "rules: {from: a}",
        "rules:\n  - from: a\n    action: allow\n",
        "rules:\n  - {from: a, to: b, action: maybe}\n",
    ],
)
def test_malformed_files_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_rules(text)


def _conversation(session_id: str, project: str, alias: str | None) -> SimpleNamespace:
    labels = {PROJECT_LABEL_KEY: project} if project else {}
    if alias:
        labels[ALIAS_LABEL_KEY] = alias
    return SimpleNamespace(
        id=session_id, labels=labels, project_id=None, archived=False, title="t"
    )


def test_server_resolver_names_both_ends_by_handle() -> None:
    sender = _conversation(SENDER_ID, "forthkit", None)
    target = _conversation(TARGET_ID, "touchkey", "Bench")
    store = MagicMock()
    store.get_conversation.side_effect = {SENDER_ID: sender, TARGET_ID: target}.get
    store.list_conversations.return_value = SimpleNamespace(data=[target], has_more=False)
    resolve = messaging_route_resolver(store, None)
    assert resolve(SENDER_ID, "touchkey/Bench", "user") == MessageRoute(
        "forthkit/", "touchkey/Bench"
    )
    store.list_conversations.return_value = SimpleNamespace(data=[], has_more=False)
    assert resolve(SENDER_ID, "touchkey/Nobody", "user") is None

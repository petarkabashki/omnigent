"""Messaging rules decide session-to-session messages by role handle."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.policies.builtins.messaging import (
    MessageRoute,
    add_allow_rule,
    insert_rule_text,
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


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def test_rule_past_until_is_skipped_and_live_one_applies() -> None:
    text = """
default: deny
rules:
  - {from: "a/*", to: "b/*", action: allow, until: "2026-11-09T11:00:00+00:00"}
  - {from: "a/*", to: "b/*", action: ask, until: "2026-11-09T13:00:00+00:00"}
"""
    decision = parse_rules(text).match(MessageRoute("a/X", "b/Y"), NOW)
    assert (decision.action, decision.rule_index) == ("ASK", 2)
    assert (
        parse_rules(text).match(MessageRoute("a/X", "b/Y"), NOW + timedelta(hours=2)).action
        == "DENY"
    )


def test_unquoted_yaml_timestamp_is_accepted() -> None:
    rules = parse_rules(
        "rules:\n  - {from: a, to: b, action: allow, until: 2026-11-09T11:00:00+00:00}"
    )
    assert rules.rules[0].until == datetime(2026, 11, 9, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize("until", ["2026-11-09T11:00:00", "tomorrow", "[1]"])
def test_invalid_until_is_a_parse_error(until: str) -> None:
    with pytest.raises(ValueError, match="until"):
        parse_rules(f'rules:\n  - {{from: a, to: b, action: allow, until: "{until}"}}')


def test_invalid_until_keeps_last_good_parse(tmp_path, routes) -> None:
    routes["t"] = "touchkey/X"
    _, policy = _policy(tmp_path)
    assert policy(_message("t"))["result"] == "ALLOW"
    path = tmp_path / "messaging-policy.yaml"
    path.write_text("rules:\n  - {from: '*', to: '*', action: deny, until: nope}\n")
    os.utime(path, (path.stat().st_mtime + 5,) * 2)
    assert policy(_message("t"))["result"] == "ALLOW"


def test_project_placeholder_means_the_senders_project() -> None:
    rules = parse_rules(
        'default: deny\nrules:\n  - {from: "*", to: "{project}/*", action: allow}\n'
    )
    assert rules.decide(MessageRoute("forthkit/A", "forthkit/B"))[0] == "ALLOW"
    assert rules.decide(MessageRoute("forthkit/A", "touchkey/B"))[0] == "DENY"
    assert rules.decide(MessageRoute("fo[x]/A", "fo[x]/B"))[0] == "ALLOW"


COMMENTED = """# header
default: ask   # fallback

rules:  # first match wins
  # keep this
  - from: x/*
    to: y/*
    action: deny
"""


def test_insert_puts_rule_first_and_keeps_comments() -> None:
    until = NOW + timedelta(hours=2)
    text = insert_rule_text(COMMENTED, "a/B", "c/D", "allow", until)
    assert text.startswith("# header\ndefault: ask   # fallback\n")
    assert "  # keep this\n" in text and 'rules:  # first match wins\n  - from: "a/B"' in text
    rules = parse_rules(text)
    assert [(r.sender, r.until) for r in rules.rules] == [("a/B", until), ("x/*", None)]


@pytest.mark.parametrize("start", ["", "default: ask\n", "rules: []\n", "rules:\n"])
def test_insert_handles_missing_or_empty_rules(start: str) -> None:
    rules = parse_rules(insert_rule_text(start, "a/B", "c/D", "allow"))
    assert [(r.sender, r.target, r.action) for r in rules.rules] == [("a/B", "c/D", "ALLOW")]


def test_add_allow_rule_writes_atomically_and_policy_sees_it(tmp_path, routes) -> None:
    routes["k"] = "y/Q"
    path, policy = _policy(tmp_path, COMMENTED)
    path.chmod(0o640)
    before = path.stat().st_ino
    assert policy(_message("k"))["result"] == "ASK"
    add_allow_rule(str(path), "forthkit/Product", "y/Q", hours=2)
    assert path.stat().st_ino != before  # replaced via rename
    assert path.stat().st_mode & 0o777 == 0o640
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".messaging-rules-")]
    os.utime(path, (path.stat().st_mtime + 5,) * 2)
    assert policy(_message("k"))["result"] == "ALLOW"
    assert "# keep this" in path.read_text()

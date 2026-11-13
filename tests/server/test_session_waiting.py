"""Builders that say what a session is waiting on, where, and since when."""

from __future__ import annotations

import json
import time

import pytest

from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    NewConversationItem,
)
from omnigent.runtime import pending_elicitations, pending_inputs
from omnigent.server import role_threads, session_live_state
from omnigent.server.session_waiting import build_session_waiting
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

NOW = 1_800_000_000
HOST_ID = "a" * 32


@pytest.fixture(autouse=True)
def _clean_indexes():
    pending_elicitations.reset_for_tests()
    pending_inputs.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()
    pending_inputs.reset_for_tests()


@pytest.fixture
def store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _call(call_id: str, name: str = "Bash", args: dict | None = None) -> NewConversationItem:
    data = FunctionCallData(
        agent="a",
        name=name,
        arguments=json.dumps(args or {"command": "pytest -q"}),
        call_id=call_id,
    )
    return NewConversationItem(type="function_call", response_id="r", data=data)


def _output(call_id: str) -> NewConversationItem:
    data = FunctionCallOutputData(call_id=call_id, output="ok")
    return NewConversationItem(type="function_call_output", response_id="r", data=data)


def _build(store, conv, **overrides):
    options = {"status": "running", "status_since": NOW - 60, "runner_online": True, "now": NOW}
    options.update(overrides)
    return build_session_waiting(store, store.get_conversation(conv.id), **options)


def _kinds(payload) -> list[str]:
    return [item["kind"] for item in payload["items"]]


def test_idle_session_with_nothing_pending_has_no_items(store) -> None:
    conv = store.create_conversation(kind="default")
    payload = _build(store, conv, status="idle", status_since=None)
    assert payload["items"] == []
    assert payload["waiting_reason"] is None
    assert payload["status"] == "idle"


def test_open_tool_call_is_listed_until_its_output_arrives(store) -> None:
    conv = store.create_conversation(kind="default")
    store.append(conv.id, [_call("c1"), _output("c1"), _call("c2", args={"command": "sleep 99"})])
    payload = _build(store, conv)
    assert _kinds(payload) == ["tool_call"]
    item = payload["items"][0]
    assert item["title"] == "Bash: sleep 99"
    assert item["actions"] == ["open", "stop"]
    assert item["where"] == "local"
    assert item["since"] is not None


def test_tool_calls_ignored_when_session_is_idle_or_call_is_async(store) -> None:
    conv = store.create_conversation(kind="default")
    store.append(conv.id, [_call("c1"), _call("c2", name="sys_call_async")])
    assert _build(store, conv, status="idle")["items"] == []
    assert _kinds(_build(store, conv)) == ["tool_call"]


def test_background_shells_use_status_since_and_mark_waiting_reason(store) -> None:
    conv = store.create_conversation(kind="default")
    shells = [{"id": "s1", "command": "sleep 120", "description": "Wait for CI"}, {"id": "s2"}]
    payload = _build(store, conv, status="waiting", shells=shells)
    assert _kinds(payload) == ["background_shell", "background_shell"]
    assert payload["items"][0]["since"] == NOW - 60
    assert payload["items"][0]["title"] == "Shell: sleep 120"
    assert payload["waiting_reason"] == "2 background shells"


def test_busy_children_are_subagents(store) -> None:
    parent = store.create_conversation(kind="default")
    busy = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    payload = _build(
        store, parent, child_status_of=lambda child: "running" if child.id == busy.id else "idle"
    )
    assert [(i["kind"], i["ref"]) for i in payload["items"]] == [("subagent", busy.id)]


def test_approval_and_queued_message(store) -> None:
    conv = store.create_conversation(kind="default")
    pending_elicitations.record_publish(
        conv.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "e1",
            "params": {"message": "Run rm -rf?"},
        },
    )
    pending_inputs.record(conv.id, [{"type": "input_text", "text": "hello there"}])
    payload = _build(store, conv, status="waiting")
    assert sorted(_kinds(payload)) == ["approval", "queued_message"]
    assert payload["waiting_reason"] == "1 approval"
    approval = next(i for i in payload["items"] if i["kind"] == "approval")
    assert approval["title"] == "Approval: Run rm -rf?"
    assert approval["ref"] == "e1"
    queued = next(i for i in payload["items"] if i["kind"] == "queued_message")
    assert queued["title"] == "Queued: hello there"
    assert abs(queued["since"] - time.time()) < 5


def _thread_labels(counterpart: str, state: str, at: int, **extra: str) -> dict[str, str]:
    digest = role_threads.thread_hash(counterpart)
    fields = {"cp": counterpart, "state": state, "at": str(at), "unread": "2", **extra}
    return {f"thread.{digest}.{key}": value for key, value in fields.items()}


def test_threads_waiting_on_us_listed_and_waiting_on_them_only_when_old(store) -> None:
    labels = {
        **_thread_labels("proj/Architect", "waiting_on_us", NOW - 120, nudges="1"),
        **_thread_labels("proj/Fresh", "waiting_on_them", NOW - 60),
        **_thread_labels("proj/Old", "waiting_on_them", NOW - 3600),
        **_thread_labels("proj/Done", "open", NOW - 3600),
    }
    conv = store.create_conversation(kind="default", labels=labels)
    payload = _build(store, conv, status="idle")
    assert [i["ref"] for i in payload["items"]] == ["proj/Old", "proj/Architect"]
    ours = payload["items"][1]
    assert ours["actions"] == ["open", "nudge"]
    assert ours["detail"] == "2 unhandled · waiting on us · nudged 1×"
    assert [i["stale"] for i in payload["items"]] == [True, False]
    assert [i["needs_owner"] for i in payload["items"]] == [False, False]


def test_only_held_or_owner_marked_threads_need_the_owner(store) -> None:
    labels = {
        **_thread_labels("proj/Owner", "waiting_on_owner", NOW - 60),
        **_thread_labels("proj/Held", "waiting_on_us", NOW - 90, held="ok 6", held_to="s"),
        **_thread_labels("proj/Blocked", "blocked", NOW - 120),
        **_thread_labels("proj/Silent", role_threads.NO_REPLY_STATE, NOW - 150),
        **_thread_labels("proj/Them", "waiting_on_them", NOW - 3600),
    }
    conv = store.create_conversation(kind="default", labels=labels)
    items = _build(store, conv, status="idle")["items"]
    assert sorted((i["ref"], i["needs_owner"]) for i in items) == [
        ("proj/Blocked", False),
        ("proj/Held", True),
        ("proj/Owner", True),
        ("proj/Silent", False),
        ("proj/Them", False),
    ]


def test_role_mail_without_a_holder_needs_the_owner(store) -> None:
    conv = store.create_conversation(kind="default")
    waiting = {"/Product": (2, NOW - 3600), "/Bench": (1, NOW - 60)}
    items = _build(store, conv, status="idle", waiting_holder=waiting)["items"]
    assert [(i["kind"], i["ref"], i["stale"], i["needs_owner"]) for i in items] == [
        ("waiting_holder", "/Product", True, True),
        ("waiting_holder", "/Bench", False, True),
    ]
    assert items[0]["detail"] == "2 messages wait for a holder"
    assert items[1]["detail"] == "1 message waits for a holder"


def test_runner_offline_is_always_stale_and_needs_host_and_busy_status(store) -> None:
    conv = store.create_conversation(kind="default", host_id=HOST_ID, workspace="/w")
    payload = _build(store, conv, runner_online=False)
    assert _kinds(payload) == ["runner_offline"]
    assert payload["items"][0]["where"] == HOST_ID
    assert payload["items"][0]["stale"] is True
    assert payload["items"][0]["needs_owner"] is True
    assert _build(store, conv, runner_online=False, status="idle")["items"] == []
    assert _build(store, conv, runner_online=True)["items"] == []
    assert _build(store, conv, runner_online=None)["items"] == []


def test_async_tasks_come_from_the_task_view(store, monkeypatch) -> None:
    conv = store.create_conversation(kind="default")
    monkeypatch.setattr(
        "omnigent.server.session_waiting.open_async_tasks",
        lambda _store, _id: [
            {"task_id": "h1", "tool": "slow", "since": NOW - 2400, "detail": "q=1"}
        ],
    )
    payload = _build(store, conv)
    assert [(i["kind"], i["ref"], i["stale"]) for i in payload["items"]] == [
        ("async_task", "h1", True)
    ]


def test_items_sorted_oldest_first_with_unknown_since_last(store, monkeypatch) -> None:
    conv = store.create_conversation(kind="default")
    monkeypatch.setattr(
        "omnigent.server.session_waiting.open_async_tasks",
        lambda _store, _id: [{"task_id": "h1", "tool": "t", "since": None}],
    )
    payload = _build(store, conv, status="waiting", shells=[{"id": "s1", "command": "x"}])
    assert _kinds(payload) == ["background_shell", "async_task"]


def test_one_failing_source_does_not_break_the_rest(store, monkeypatch) -> None:
    conv = store.create_conversation(kind="default")

    def boom(_store, _id):
        raise RuntimeError("nope")

    monkeypatch.setattr("omnigent.server.session_waiting.open_async_tasks", boom)
    payload = _build(store, conv, status="waiting", shells=[{"id": "s1", "command": "x"}])
    assert _kinds(payload) == ["background_shell"]


def test_stale_flag_follows_the_kind_limit(store) -> None:
    conv = store.create_conversation(kind="default")
    fresh = _build(store, conv, status="waiting", shells=[{"id": "s"}], status_since=NOW - 29 * 60)
    old = _build(store, conv, status="waiting", shells=[{"id": "s"}], status_since=NOW - 31 * 60)
    assert fresh["items"][0]["stale"] is False
    assert old["items"][0]["stale"] is True


def test_status_since_records_only_real_transitions(store, monkeypatch) -> None:
    session_live_state.configure(store)
    try:
        clock = [100.0]
        monkeypatch.setattr(session_live_state.time, "time", lambda: clock[0])
        session_live_state.persist_live_status("s1", "running")
        clock[0] = 150.0
        session_live_state.persist_live_status("s1", "running")
        assert session_live_state.status_since("s1") == 100
        clock[0] = 200.0
        session_live_state.persist_live_status("s1", "idle")
        assert session_live_state.status_since("s1") == 200
        session_live_state.forget_live_status("s1")
        assert session_live_state.status_since("s1") is None
        # Another replica, or a restarted hub, reads the row's stamp.
        assert session_live_state.status_since("s1", 180) == 180
    finally:
        session_live_state.configure(None)

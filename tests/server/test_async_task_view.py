"""Open async tasks derived from persisted sys_call_async items."""

from __future__ import annotations

import json

from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.server.async_task_view import open_async_tasks
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _dispatch(call_id: str, handle_id: str, tool: str = "slow", args: str = '{"q": 1}') -> list:
    call = FunctionCallData(
        agent="a",
        name="sys_call_async",
        arguments=json.dumps({"tool": tool, "args": args}),
        call_id=call_id,
    )
    handle = {"handle_id": handle_id, "tool_name": tool, "status": "in_progress"}
    output = FunctionCallOutputData(call_id=call_id, output=json.dumps(handle))
    return [
        NewConversationItem(type="function_call", response_id="r", data=call),
        NewConversationItem(type="function_call_output", response_id="r", data=output),
    ]


def _inbox_line(text: str) -> NewConversationItem:
    data = MessageData(role="user", content=[{"type": "input_text", "text": text}])
    return NewConversationItem(type="message", response_id="r", data=data)


def _cancel(call_id: str, handle_id: str) -> NewConversationItem:
    data = FunctionCallData(
        agent="a",
        name="sys_cancel_async",
        arguments=json.dumps({"handle_id": handle_id}),
        call_id=call_id,
    )
    return NewConversationItem(type="function_call", response_id="r", data=data)


def _session(db_uri: str, *batches: list) -> tuple[SqlAlchemyConversationStore, str]:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = store.create_conversation(kind="default").id
    for batch in batches:
        store.append(session_id, batch)
    return store, session_id


def test_no_dispatches_means_no_open_tasks(db_uri: str) -> None:
    store, session_id = _session(db_uri, [_inbox_line("hello")])
    assert open_async_tasks(store, session_id) == []


def test_dispatched_task_is_open(db_uri: str) -> None:
    store, session_id = _session(db_uri, _dispatch("c1", "h1", tool="slow", args='{"q":  1}'))
    [task] = open_async_tasks(store, session_id)
    assert task["task_id"] == "h1"
    assert task["tool"] == "slow"
    assert task["detail"] == '{"q": 1}'
    assert isinstance(task["since"], int)


def test_completed_task_is_closed_and_others_stay_open(db_uri: str) -> None:
    store, session_id = _session(
        db_uri,
        _dispatch("c1", "h1"),
        _dispatch("c2", "h2"),
        [_inbox_line("[System: task h1 completed — slow returned: ok]")],
    )
    assert [task["task_id"] for task in open_async_tasks(store, session_id)] == ["h2"]


def test_cancelled_task_is_closed(db_uri: str) -> None:
    store, session_id = _session(
        db_uri, _dispatch("c1", "h1"), _dispatch("c2", "h2"), [_cancel("c3", "h1")]
    )
    assert [task["task_id"] for task in open_async_tasks(store, session_id)] == ["h2"]
    store.append(session_id, [_inbox_line("[System: task h2 cancelled]")])
    assert open_async_tasks(store, session_id) == []

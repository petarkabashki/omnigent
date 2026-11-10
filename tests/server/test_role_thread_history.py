"""Role thread history: newest first, both sides in order, and folding ``session:<id>`` rows."""

from __future__ import annotations

import itertools

import pytest

from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.server import role_threads
from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture()
def store(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> SqlAlchemyConversationStore:
    # One clock tick per write keeps message order unambiguous.
    clock = itertools.count(1_000_000)
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: next(clock))
    return SqlAlchemyConversationStore(db_uri)


def _say(store: SqlAlchemyConversationStore, session_id: str, *texts: str) -> None:
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_test",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for text in texts
        ],
    )


def _from(handle: str, thread: str | None, text: str, n: int = 1) -> str:
    tag = f" [thread {thread}]" if thread else ""
    return f"[Message from {handle} (session s, message msg_{n:032x})]{tag}\n{text}"


def _conv(store: SqlAlchemyConversationStore, session_id: str) -> Conversation:
    conversation = store.get_conversation(session_id)
    assert conversation is not None
    return conversation


def _role(store: SqlAlchemyConversationStore, handle: str) -> str:
    return store.create_conversation(labels={role_threads.THREAD_ROLE_LABEL: handle}).id


def test_history_reaches_the_newest_messages_of_a_long_session(
    store: SqlAlchemyConversationStore,
) -> None:
    holder = _role(store, "p/Architect")
    _say(store, holder, _from("p/Product", "p/Product", "oldest", 1))
    _say(store, holder, *(f"chatter {index}" for index in range(2100)))
    _say(store, holder, _from("p/Product", "p/Product", "newest", 2))

    conv = _conv(store, holder)
    rows = role_threads.thread_history(store, conv, "p/Product", user_id=None, limit=50)
    assert [row["text"] for row in rows] == ["oldest", "newest"]
    one = role_threads.thread_history(store, conv, "p/Product", user_id=None, limit=1)
    assert [row["text"] for row in one] == ["newest"]


def test_conversation_marks_both_sides_in_time_order(store: SqlAlchemyConversationStore) -> None:
    architect = _role(store, "p/Architect")
    product = _role(store, "p/Product")
    unroled = store.create_conversation().id
    _say(store, architect, _from("p/Product", "p/Product", "ask", 1))
    _say(store, product, _from("p/Architect", "p/Architect", "answer", 2))
    owner = "[Message from owner (message msg_" + "f" * 32 + ")] [thread p/Product]\nowner says"
    _say(store, architect, owner)
    _say(store, product, owner.replace("[thread p/Product]", "[thread p/Architect]"))
    _say(store, architect, _from("p/Product", "p/Product", "thanks", 3))
    # Sent before the architect took its role, so keyed to its session.
    _say(store, product, _from(architect, f"session:{architect}", "early", 4))
    _say(store, unroled, _from("p/Architect", None, "to unroled", 5))

    conv = _conv(store, architect)
    rows = role_threads.thread_conversation(store, conv, "p/Product", user_id=None, limit=50)
    assert [(row["direction"], row["text"]) for row in rows] == [
        ("in", "ask"),
        ("out", "answer"),
        ("owner", "owner says"),
        ("in", "thanks"),
        ("out", "early"),
    ]
    sent = role_threads.thread_conversation(
        store,
        conv,
        f"session:{unroled}",
        user_id=None,
        limit=50,
    )
    assert [(row["direction"], row["text"]) for row in sent] == [("out", "to unroled")]


def _row(store: SqlAlchemyConversationStore, holder: str, key: str) -> dict:
    labels = _conv(store, holder).labels
    return next(row for row in role_threads.thread_index(labels) if row["counterpart"] == key)


def test_fold_merges_session_row_into_role_without_double_counting(
    store: SqlAlchemyConversationStore,
) -> None:
    holder = _role(store, "p/Architect")
    late = store.create_conversation().id
    split = f"session:{late}"
    role_threads.record_inbound(store, _conv(store, holder), split, None, "m1", "hi", 1, now=10)
    role_threads.record_inbound(store, _conv(store, holder), split, None, "m2", "again", 1, now=20)
    role_threads.record_inbound(
        store, _conv(store, holder), "p/Product", None, "m3", "as role", 1, now=30
    )
    before = _row(store, holder, "p/Product")["messages"] + _row(store, holder, split)["messages"]

    store.set_labels(late, {"omnigent.alias": "Product"})
    assert role_threads.fold_session_threads(store, late, "p/Product", user_id=None) == 1
    assert role_threads.fold_session_threads(store, late, "p/Product", user_id=None) == 0
    assert role_threads.fold_session_thread(store, holder, late, "p/Product") is False

    labels = _conv(store, holder).labels
    keys = [row["counterpart"] for row in role_threads.thread_index(labels)]
    assert keys == ["p/Product"]
    merged = _row(store, holder, "p/Product")
    assert merged["messages"] == before == 3
    assert merged["unread"] == 3
    assert merged["summary"] == "as role"


def test_fold_into_missing_role_row_moves_it_whole(store: SqlAlchemyConversationStore) -> None:
    holder = _role(store, "p/Architect")
    late = store.create_conversation().id
    role_threads.record_inbound(
        store, _conv(store, holder), f"session:{late}", None, "m1", "hi", 1, now=10
    )
    assert role_threads.fold_session_thread(store, holder, late, "p/Product") is True
    row = _row(store, holder, "p/Product")
    assert (row["messages"], row["unread"], row["summary"]) == (1, 1, "hi")

"""Write-hook coverage: every committed write to an observed table carries a write-hook note.

The guard (tests/stores/write_hook_guard.py) is opted into by the store test modules whose writes
are hooked. These tests check the guard itself, and the note/observer contract it relies on.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.orm import Session

from omnigent.db import write_hooks
from omnigent.db.db_models import SqlConversation, SqlConversationLabel, SqlConversationMetadata
from omnigent.db.utils import make_named_managed_session_maker, run_write_transaction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.util.uuid import uuid_to_bytes
from tests.stores.write_hook_guard import WriteHookGuard


@pytest.fixture()
def store(tmp_path: Path) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(f"sqlite:///{tmp_path}/hub.db")


@pytest.fixture()
def guard():
    g = WriteHookGuard(allow_test_writes=False)
    g.install()
    yield g
    g.uninstall()


@pytest.fixture()
def notes():
    seen: list[write_hooks.WriteNote] = []
    previous = write_hooks.set_observer(seen.extend)
    yield seen
    write_hooks.set_observer(previous)


def _maker(store: SqlAlchemyConversationStore):
    return make_named_managed_session_maker(
        store._conv_engine, query_name_prefix="tests.write_hooks", immediate=True
    )


def _raw_label(session, cid: str, key: str) -> None:
    """Raw SQL: invisible to the automatic listeners, so it needs an explicit note."""
    session.execute(
        text(
            "INSERT INTO conversation_labels"
            " (workspace_id, conversation_id, key, value, updated_at)"
            " VALUES (0, :cid, :key, 'v', 0)"
        ),
        {"cid": uuid_to_bytes(cid), "key": key},
    )


def _label(session, cid: str, key: str) -> None:
    session.execute(
        insert(SqlConversationLabel).values(conversation_id=cid, key=key, value="v", updated_at=0)
    )


def test_guard_reports_an_unnoted_write_with_its_call_site(store, guard) -> None:
    cid = store.create_conversation(title="t").id
    run_write_transaction(_maker(store), "unnoted", lambda s: _raw_label(s, cid, "k"))
    ((table, inner, _outer),) = guard.violations
    assert table == "conversation_labels"
    assert inner.startswith("tests/stores/test_write_hook_coverage.py:") and inner.endswith(
        " in _raw_label"
    )
    assert "write_hooks.note(" in guard.report()


def test_guard_accepts_a_noted_write(store, guard) -> None:
    cid = store.create_conversation(title="t").id

    def write(session) -> None:
        _raw_label(session, cid, "k")
        write_hooks.note(
            session, SqlConversationLabel, "insert", [{"conversation_id": cid, "key": "k"}]
        )

    run_write_transaction(_maker(store), "noted", write)
    assert guard.violations == {}


def test_guard_ignores_a_rolled_back_write(store, guard) -> None:
    cid = store.create_conversation(title="t").id

    def write(session) -> None:
        _raw_label(session, cid, "k")
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError):
        run_write_transaction(_maker(store), "rolled_back", write)
    assert guard.violations == {}


def test_notes_reach_the_observer_only_after_commit(store, notes) -> None:
    cid = store.create_conversation(title="t").id
    notes.clear()

    def failing(session) -> None:
        _label(session, cid, "a")
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError):
        run_write_transaction(_maker(store), "failing", failing)
    assert notes == []

    run_write_transaction(_maker(store), "ok", lambda s: _label(s, cid, "b"))
    assert notes == [
        (
            "conversation_labels",
            "insert",
            [{"workspace_id": 0, "conversation_id": cid, "key": "b"}],
        )
    ]


def test_flushed_objects_are_noted_by_primary_key(store, notes) -> None:
    cid = store.create_conversation(title="t").id
    assert ("conversations", "insert", [{"workspace_id": 0, "id": cid}]) in notes
    assert ("omnigent_conversation_metadata", "insert", [{"workspace_id": 0, "id": cid}]) in notes
    notes.clear()

    def rename(session) -> None:
        session.get(SqlConversation, (0, cid)).title = "renamed"

    run_write_transaction(_maker(store), "rename", rename)
    assert notes == [("conversations", "update", [{"workspace_id": 0, "id": cid}])]


def test_bulk_statements_are_noted_with_the_rows_they_match(store, notes) -> None:
    cid = store.create_conversation(title="t", labels={"a": "1", "b": "2"}).id
    notes.clear()
    where = (SqlConversationLabel.conversation_id == cid,)
    run_write_transaction(
        _maker(store),
        "bulk_delete",
        lambda s: s.execute(delete(SqlConversationLabel).where(*where)),
    )
    ((table, op, keys),) = notes
    assert (table, op) == ("conversation_labels", "delete")
    assert sorted(k["key"] for k in keys) == ["a", "b"]


def test_note_without_a_primary_key_column_is_keyless(store, notes) -> None:
    cid = store.create_conversation(title="t").id
    notes.clear()

    def write(session) -> None:
        _raw_label(session, cid, "k")
        write_hooks.note(session, SqlConversationLabel, "insert", [{"conversation_id": cid}])

    run_write_transaction(_maker(store), "keyless", write)
    assert notes == [("conversation_labels", "insert", None)]


def test_note_matching_names_the_rows_a_raw_delete_removes(store, notes) -> None:
    cid = store.create_conversation(title="t", labels={"a": "1", "b": "2"}).id
    notes.clear()

    def write(session) -> None:
        write_hooks.note_matching(
            session, SqlConversationLabel, "delete", SqlConversationLabel.conversation_id == cid
        )
        session.execute(
            text("DELETE FROM conversation_labels WHERE conversation_id = :cid"),
            {"cid": uuid_to_bytes(cid)},
        )

    run_write_transaction(_maker(store), "raw_delete", write)
    ((table, op, keys),) = notes
    assert (table, op, sorted(k["key"] for k in keys)) == (
        "conversation_labels",
        "delete",
        ["a", "b"],
    )


def test_guard_reports_notes_committed_outside_managed_session(store, guard) -> None:
    cid = store.create_conversation(title="t").id
    with Session(store._conv_engine) as session:
        _label(session, cid, "k")
        session.commit()
    ((tables, site),) = guard.undelivered.values()
    assert tables == ["conversation_labels"]
    assert site.startswith("tests/stores/test_write_hook_coverage.py:")
    assert "never delivered" in guard.report()


def test_a_cleared_observer_collects_nothing(store) -> None:
    previous = write_hooks.set_observer(lambda notes: None)
    write_hooks.set_observer(None)
    assert not write_hooks.observing()
    cid = store.create_conversation(title="t").id

    def write(session) -> None:
        write_hooks.note(session, SqlConversation, "update", [{"id": cid}])
        session.get(SqlConversationMetadata, (0, cid)).task_summary = "changed"
        session.flush()
        session.execute(delete(SqlConversationLabel).where(SqlConversationLabel.key == "x"))
        assert write_hooks._NOTES_KEY not in session.info

    run_write_transaction(_maker(store), "no_observer", write)
    write_hooks.set_observer(previous)


def test_switching_the_observer_under_concurrent_writers(store) -> None:
    cids = [store.create_conversation(title=f"t{i}").id for i in range(4)]
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(cid: str) -> None:
        n = 0
        while not stop.is_set():
            try:
                store.set_labels(cid, {"n": str(n)})
            except BaseException as exc:
                errors.append(exc)
                return
            n += 1

    threads = [threading.Thread(target=writer, args=(cid,)) for cid in cids]
    for t in threads:
        t.start()
    seen: list[write_hooks.WriteNote] = []
    try:
        for _ in range(200):
            write_hooks.set_observer(seen.extend)
            write_hooks.set_observer(None)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert errors == []

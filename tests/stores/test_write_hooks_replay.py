"""Replaying write-hook notes reproduces the hub: an observer that reads back every noted key after
commit keeps a replica equal to the database, through creates, appends, moves, forks and deletes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.types import TypeDecorator

from omnigent.db import db_models, write_hooks
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store import sqlalchemy_store as store_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

_TABLES = {
    model.__table__.name: model.__table__
    for model in vars(db_models).values()
    if getattr(getattr(model, "__table__", None), "name", None) in write_hooks.OBSERVED_TABLES
}


class Replica:
    """Observer that mirrors the observed tables by reading back each noted key after commit."""

    def __init__(self, uri: str) -> None:
        self.engine = create_engine(uri)
        self.gaps: list[str] = []
        self.rows = self.snapshot()

    def snapshot(self) -> dict[str, dict[tuple, dict[str, Any]]]:
        with self.engine.connect() as conn:
            return {
                name: {
                    self._pk(table, row._mapping): dict(row._mapping)
                    for row in conn.execute(select(table))
                }
                for name, table in _TABLES.items()
            }

    def _pk(self, table, values) -> tuple:
        out = []
        for column in table.primary_key.columns:
            value, kind = values[column.name], column.type
            if isinstance(kind, TypeDecorator):  # the stored form, as a read returns it
                dialect = self.engine.dialect
                value = kind.process_result_value(kind.process_bind_param(value, dialect), dialect)
            out.append(value)
        return tuple(out)

    def __call__(self, notes: list[write_hooks.WriteNote]) -> None:
        with self.engine.connect() as conn:
            for name, _op, keys in notes:
                if keys is None:
                    self.gaps.append(name)
                    continue
                table = _TABLES[name]
                for key in keys:
                    pk = self._pk(table, key)
                    where = [c == v for c, v in zip(table.primary_key.columns, pk, strict=True)]
                    row = conn.execute(select(table).where(*where)).first()
                    if row is None:
                        self.rows[name].pop(pk, None)
                    else:
                        self.rows[name][pk] = dict(row._mapping)


@pytest.fixture()
def uri(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path}/hub.db"


@pytest.fixture()
def store(uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(uri)


@pytest.fixture()
def replica(store, uri: str):
    replica = Replica(uri)
    previous = write_hooks.set_observer(replica)
    yield replica
    write_hooks.set_observer(previous)


def _assert_replica_matches_hub(replica: Replica) -> None:
    assert replica.gaps == []
    assert replica.rows == replica.snapshot()


def _input(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=PENDING_INPUT_RESPONSE_ID,
        status="in_progress",
        stable_id=uuid4().hex,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def test_session_lifecycle_replays_to_the_hub(store, uri, replica) -> None:
    source = store.create_conversation(title="source", labels={"a": "1"})
    target = store.create_conversation(title="target")
    first, second = store.append(source.id, [_input("first"), _input("second")])
    store.set_labels(source.id, {"a": "2", "b": "3"})
    store.delete_label(source.id, "b")
    store.update_conversation(source.id, title="renamed")
    store.move_pending_inputs(source.id, target.id, [first.id])
    fork = store.fork_conversation(source.id)
    _assert_replica_matches_hub(replica)

    # Child rows another store wrote: delete_conversation removes them with the session.
    perms = SqlAlchemyPermissionStore(uri)
    perms.ensure_user("owner@example.com")
    perms.grant("owner@example.com", source.id, 4)
    SqlAlchemyPolicyStore(uri).create(
        policy_id=uuid4().hex, session_id=source.id, name="p", type="python", handler="m.f"
    )
    replica.rows = replica.snapshot()  # those stores are not hooked yet

    assert asyncio.run(store.delete_conversation(source.id)) is True
    assert asyncio.run(store.delete_conversation(fork.id)) is True
    assert second.id not in {pk[-2] for pk in replica.rows["conversation_items"]}
    _assert_replica_matches_hub(replica)


def test_a_create_failing_in_its_second_transaction_records_exactly_what_committed(
    store, replica, monkeypatch
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(store_module, "lock_host_for_binding", fail)
    before = len(replica.rows["conversations"])
    with pytest.raises(RuntimeError):
        store.create_conversation(title="half", labels={"a": "1"})
    # The first transaction committed the conversation and its labels; the second wrote nothing.
    assert len(replica.rows["conversations"]) == before + 1
    _assert_replica_matches_hub(replica)


def test_a_create_discarded_after_a_conflict_replays_to_the_hub(
    store, replica, monkeypatch
) -> None:
    def conflict(*_args, **_kwargs):
        raise OmnigentError("host busy", code=ErrorCode.CONFLICT)

    monkeypatch.setattr(store_module, "lock_host_for_binding", conflict)
    before = len(replica.rows["conversations"])
    with pytest.raises(OmnigentError):
        store.create_conversation(title="discarded", labels={"a": "1"})
    assert len(replica.rows["conversations"]) == before
    _assert_replica_matches_hub(replica)

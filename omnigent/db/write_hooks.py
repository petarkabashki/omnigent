"""Report committed writes on observed tables to an optional, in-process write observer.

While an observer is set, two session listeners note the primary keys each write transaction
touches on an observed table: ORM flushes (added, changed and deleted objects) and ORM-enabled
``session.execute`` of insert/update/delete statements (an update or delete first selects the keys
its WHERE clause matches). Writes those listeners cannot see, such as raw SQL text, call
:func:`note` or :func:`note_matching` themselves. After the transaction commits, the managed
session hands the notes to the observer set with :func:`set_observer`. A rolled-back transaction's
notes are dropped with its session, so the observer sees exactly what committed.

Until an observer is first set, no listener is registered. Afterwards the listeners stay
registered and return at once while the observer is None; :func:`note` does the same.
The observer runs on the committing thread after the commit and must be cheap; it reads rows back
on its own connection, never on the store's.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import ORMExecuteState, Session, UOWTransaction

from omnigent.db.db_models import current_workspace_id

_logger = logging.getLogger(__name__)

# Tables whose writes must be noted. A write to one of these outside a noted transaction is a
# coverage bug: the write-hook coverage guard (tests/stores/write_hook_guard.py) fails on it.
OBSERVED_TABLES: frozenset[str] = frozenset(
    {
        "agents",
        "conversation_items",
        "conversation_labels",
        "conversations",
        "group_thread_members",
        "group_thread_posts",
        "group_threads",
        "hosts",
        "omnigent_conversation_metadata",
        "policies",
        "projects",
        "role_events",
        "roles",
        "scheduled_task_runs",
        "scheduled_tasks",
        "session_permissions",
        "users",
    }
)

WRITE_OPS = ("insert", "update", "delete")

#: One committed write: (table, op, primary keys). ``keys`` is None when the keys could not be
#: named; the observer must then record a gap for the table rather than drop the write.
WriteNote = tuple[str, str, list[dict[str, Any]] | None]
Observer = Callable[[list[WriteNote]], None]

_NOTES_KEY = "omnigent.write_hooks.notes"
_observer: Observer | None = None


def set_observer(observer: Observer | None) -> Observer | None:
    """Install ``observer`` (None removes it). Returns the previous one."""
    global _observer
    if observer is not None:
        _register_listeners()
    previous, _observer = _observer, observer
    return previous


def observing() -> bool:
    """True when an observer is installed. Lets a caller skip building keys it would not need."""
    return _observer is not None


def _table_of(model: Any) -> Any:
    return getattr(model, "__table__", model)


def _add(session: Session, table: str, op: str, keys: list[dict[str, Any]] | None) -> None:
    session.info.setdefault(_NOTES_KEY, []).append((table, op, keys))


def note(session: Session, model: Any, op: str, keys: Iterable[Mapping[str, Any]]) -> None:
    """Note that this transaction writes rows of ``model`` with these primary keys.

    ``workspace_id`` is filled from the current workspace when a key leaves it out. A key that
    misses another primary-key column is noted as keyless, so the observer records a gap.
    """
    if _observer is None:
        return
    table = _table_of(model)
    columns = [c.name for c in table.primary_key.columns]
    workspace = current_workspace_id()
    out: list[dict[str, Any]] = []
    for key in keys:
        full = {c: key.get(c, workspace if c == "workspace_id" else _MISSING) for c in columns}
        if any(v is _MISSING for v in full.values()):
            _add(session, table.name, op, None)
            return
        out.append(full)
    if out:
        _add(session, table.name, op, out)


def note_matching(session: Session, model: Any, op: str, *criteria: Any) -> None:
    """Note the rows of ``model`` that match ``criteria``, by selecting their primary keys.

    Call it before the UPDATE or DELETE that uses the same criteria. It runs a query only when an
    observer is installed; if that query fails, the table is noted as keyless instead.
    """
    if _observer is None:
        return
    table = _table_of(model)
    columns = list(table.primary_key.columns)
    try:
        rows = session.execute(select(*columns).where(*criteria)).all()
    except Exception:
        _logger.warning("write_hooks: could not select keys for %s", table.name, exc_info=True)
        _add(session, table.name, op, None)
        return
    if rows:
        _add(
            session,
            table.name,
            op,
            [dict(zip((c.name for c in columns), row, strict=True)) for row in rows],
        )


def _after_flush(session: Session, _flush_context: UOWTransaction) -> None:
    if _observer is None:
        return
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    keyless: set[tuple[str, str]] = set()
    for op, objects in (
        ("insert", session.new),
        ("update", session.dirty),
        ("delete", session.deleted),
    ):
        for obj in objects:
            mapper = sa_inspect(obj).mapper
            table = mapper.local_table
            if table.name not in OBSERVED_TABLES:
                continue
            if op == "update" and not session.is_modified(obj, include_collections=False):
                continue
            values = mapper.primary_key_from_instance(obj)
            slot = (table.name, op)
            if any(v is None for v in values):
                keyless.add(slot)
            else:
                grouped.setdefault(slot, []).append(
                    {c.name: v for c, v in zip(mapper.primary_key, values, strict=True)}
                )
    for slot in keyless:
        grouped.pop(slot, None)
        _add(session, *slot, None)
    for (table_name, op), keys in grouped.items():
        _add(session, table_name, op, keys)


def _column_name(column: Any) -> str:
    return column if isinstance(column, str) else column.key


def _bound(value: Any) -> Any:
    return getattr(value, "value", value)


def _insert_keys(state: ORMExecuteState, table: Any) -> list[dict[str, Any]] | None:
    params = state.parameters
    stmt: Any = state.statement
    rows: list[Any]
    if params:
        rows = params if isinstance(params, list) else [params]
    elif getattr(stmt, "_multi_values", None):
        rows = [dict(row) for row in stmt._multi_values[0]]
    elif getattr(stmt, "_values", None):
        rows = [dict(stmt._values)]
    else:
        return None
    names = [c.name for c in table.primary_key.columns]
    workspace = current_workspace_id()
    keys = []
    for row in rows:
        by_name = {_column_name(k): _bound(v) for k, v in row.items()}
        key = {n: by_name.get(n, workspace if n == "workspace_id" else None) for n in names}
        if any(v is None for v in key.values()):
            return None
        keys.append(key)
    return keys


def _before_statement(state: ORMExecuteState) -> None:
    if _observer is None or not (state.is_insert or state.is_update or state.is_delete):
        return
    table: Any = getattr(state.statement, "table", None)
    if getattr(table, "name", None) not in OBSERVED_TABLES:
        return
    op = "insert" if state.is_insert else "update" if state.is_update else "delete"
    if op == "insert":
        try:
            keys = _insert_keys(state, table)
        except Exception:
            _logger.warning(
                "write_hooks: could not read insert keys for %s", table.name, exc_info=True
            )
            keys = None
        if keys != []:
            _add(state.session, table.name, op, keys)
        return
    where = getattr(state.statement, "whereclause", None)
    note_matching(state.session, table, op, *(() if where is None else (where,)))


_registered = False
_register_lock = threading.Lock()


def _register_listeners() -> None:
    """Register the session listeners once, on the first observer; they are never removed.

    Removing listeners while other threads flush is not safe, so a cleared observer leaves them
    in place and each one returns at its None check.
    """
    global _registered
    with _register_lock:
        if _registered:
            return
        event.listen(Session, "after_flush", _after_flush)
        event.listen(Session, "do_orm_execute", _before_statement)
        _registered = True


def committed(session: Session) -> None:
    """Hand a committed session's notes to the observer. Never raises into the caller."""
    observer = _observer
    notes = session.info.pop(_NOTES_KEY, None)
    if observer is None or not notes:
        return
    try:
        observer(notes)
    except Exception:
        _logger.warning("write_hooks: observer failed", exc_info=True)


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()

"""Test-only guard: fail when a write to an observed table commits without a write-hook note.

A test module opts in with::

    from tests.stores.write_hook_guard import write_hook_guard  # noqa: F401

    pytestmark = pytest.mark.usefixtures("write_hook_guard")

The guard installs a no-op observer, which turns on the automatic write-hook listeners, maps each
session to the connection it begins on, and watches the SQL those connections run. When a session
commits, every observed table it wrote must be among the tables it noted. What it catches are
writes the listeners cannot see: raw SQL text and writes on a bare connection. Production code is
untouched: the guard's listeners exist only while the fixture is active.
"""

from __future__ import annotations

import re
import sys
import threading
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from omnigent.db import write_hooks

_WRITE_SQL = re.compile(
    r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+[\"`]?(\w+)",
    re.IGNORECASE,
)

_REPO = Path(__file__).resolve().parents[2]
_REL_CACHE: dict[str, str] = {}

# Writes deliberately left unhooked, by call-site path prefix. Each entry needs a reason.
ALLOWED_UNHOOKED: dict[str, str] = {
    # Schema migrations rewrite tables wholesale; the observer records them as one migration gap.
    "omnigent/db/migrations/": "schema migration",
    # Stores not hooked yet: each entry is removed by that store's hook change.
    "omnigent/stores/agent_store/": "pending: agent store hooks",
    "omnigent/stores/group_thread_store/": "pending: group thread store hooks",
    "omnigent/stores/host_store.py": "pending: host store hooks",
    "omnigent/stores/permission_store/": "pending: permission store hooks",
    "omnigent/stores/policy_store/": "pending: policy store hooks",
    "omnigent/stores/project_store/": "pending: project store hooks",
    "omnigent/stores/role_store/": "pending: role store hooks",
    "omnigent/stores/scheduled_task_store/": "pending: scheduled task store hooks",
    "omnigent/stores/session_pull_request_store.py": "pending: session pull request store hooks",
    "omnigent/server/accounts_store.py": "pending: account store hooks",
    "omnigent/server/identity_migration.py": "pending: account store hooks",
}

_NOT_A_CALL_SITE = (
    "omnigent/db/utils.py",
    "omnigent/db/write_hooks.py",
    "tests/stores/write_hook_guard.py",
)

_HINT = (
    "Add write_hooks.note(session, <Model>, '<insert|update|delete>', keys) (or note_matching "
    "before a bulk UPDATE/DELETE) in the same transaction; see omnigent/db/write_hooks.py."
)


def _repo_relative(filename: str) -> str | None:
    rel = _REL_CACHE.get(filename)
    if rel is None:
        try:
            rel = Path(filename).resolve().relative_to(_REPO).as_posix()
        except ValueError:
            rel = ""
        _REL_CACHE[filename] = rel
    return rel or None


def _call_site() -> tuple[str, str]:
    """(innermost, outermost) repo frame of the current write: the writing code and its caller."""
    frames = []
    frame = sys._getframe(1)
    while frame is not None:
        rel = _repo_relative(frame.f_code.co_filename)
        if (
            rel
            and rel.startswith(("omnigent/", "tests/"))
            and not rel.startswith(_NOT_A_CALL_SITE)
        ):
            frames.append(f"{rel}:{frame.f_lineno} in {frame.f_code.co_name}")
        frame = frame.f_back
    if not frames:
        return "<outside the repo>", "<outside the repo>"
    return frames[0], frames[-1]


class WriteHookGuard:
    """Collects unhooked writes while installed.

    ``violations`` maps (table, innermost call site, outermost call site) to a count. Writes made
    directly by test code are allowed unless ``allow_test_writes`` is False.
    """

    def __init__(self, *, allow_test_writes: bool = True) -> None:
        self.allow_test_writes = allow_test_writes
        # Sessions that committed notes which never reached write_hooks.committed: a commit
        # outside managed_session, whose notes would be dropped. Maps session to commit site.
        self.undelivered: dict[Session, tuple[list[str], str]] = {}
        self.violations: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()
        self._session_of: weakref.WeakKeyDictionary[Connection, Session] = (
            weakref.WeakKeyDictionary()
        )
        self._writes: weakref.WeakKeyDictionary[Session, list[tuple[str, str, str]]] = (
            weakref.WeakKeyDictionary()
        )

    # -- listeners
    def after_begin(self, session: Session, transaction, connection: Connection) -> None:
        self._session_of[connection] = session

    def after_cursor_execute(
        self, conn, cursor, statement, parameters, context, executemany
    ) -> None:
        match = _WRITE_SQL.match(statement)
        if not match or match.group(1) not in write_hooks.OBSERVED_TABLES:
            return
        if cursor.rowcount == 0:  # matched no rows: nothing was written
            return
        inner, outer = _call_site()
        if any(inner.startswith(prefix) for prefix in ALLOWED_UNHOOKED):
            return
        # Tests seed rows directly to reach states the stores cannot; that is not a store write.
        if self.allow_test_writes and inner.startswith("tests/"):
            return
        write = (match.group(1), inner, outer)
        session = self._session_of.get(conn)
        if session is None:  # a raw connection write: no session can note it
            self._count(write)
            return
        self._writes.setdefault(session, []).append(write)

    def after_commit(self, session: Session) -> None:
        pending = session.info.get(write_hooks._NOTES_KEY)
        if pending:
            site = _call_site()[0]
            if not (self.allow_test_writes and site.startswith("tests/")):
                with self._lock:
                    self.undelivered[session] = (sorted({t for t, _op, _k in pending}), site)
        writes = self._writes.pop(session, None)
        if not writes:
            return
        noted = {table for table, _op, _keys in session.info.get(write_hooks._NOTES_KEY, ())}
        for write in writes:
            if write[0] not in noted:
                self._count(write)

    def after_rollback(self, session: Session) -> None:
        self._writes.pop(session, None)

    def _count(self, write: tuple[str, str, str]) -> None:
        with self._lock:
            self.violations[write] = self.violations.get(write, 0) + 1

    def _committed(self, session: Session) -> None:
        with self._lock:
            self.undelivered.pop(session, None)
        self._real_committed(session)

    # -- lifecycle
    def install(self) -> None:
        self._previous = write_hooks.set_observer(lambda notes: None)
        self._real_committed = write_hooks.committed
        write_hooks.committed = self._committed
        event.listen(Session, "after_begin", self.after_begin)
        event.listen(Session, "after_commit", self.after_commit)
        event.listen(Session, "after_rollback", self.after_rollback)
        event.listen(Engine, "after_cursor_execute", self.after_cursor_execute)

    def uninstall(self) -> None:
        event.remove(Engine, "after_cursor_execute", self.after_cursor_execute)
        event.remove(Session, "after_rollback", self.after_rollback)
        event.remove(Session, "after_commit", self.after_commit)
        event.remove(Session, "after_begin", self.after_begin)
        write_hooks.committed = self._real_committed
        write_hooks.set_observer(self._previous)

    def report(self) -> str:
        lines = [
            f"{table} written without a write-hook note at {inner} (called from {outer}), {count}x"
            for (table, inner, outer), count in sorted(self.violations.items())
        ]
        lines += [
            f"{', '.join(tables)} noted but never delivered: committed at {site} outside "
            "managed_session, so the observer would not see it"
            for tables, site in self.undelivered.values()
        ]
        return "\n".join([*lines, _HINT])


@pytest.fixture()
def write_hook_guard() -> Iterator[WriteHookGuard]:
    """Fail the test if it commits a write to an observed table that no write-hook note covers."""
    guard = WriteHookGuard()
    guard.install()
    try:
        yield guard
    finally:
        guard.uninstall()
    if guard.violations or guard.undelivered:
        pytest.fail(guard.report(), pytrace=False)

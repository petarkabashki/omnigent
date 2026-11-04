"""Tests for :class:`SqlAlchemyRoleStore`: uniqueness, the CAS and the event log.

Run on SQLite and, with ``OMNIGENT_TEST_DB_URI`` set, on that server. SQLite
serializes writers, so races are forced with the store's test seams: two writers
are lined up at one generation before either opens its transaction.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

import pytest

from omnigent.entities import Role
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.role_store.sqlalchemy_store import SqlAlchemyRoleStore

OWNER = "alice"


def _id() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def roles(db_uri: str) -> SqlAlchemyRoleStore:
    return SqlAlchemyRoleStore(db_uri)


def _take(roles: SqlAlchemyRoleStore, role: Role, session_id: str) -> Role | None:
    return roles.take(
        role,
        session_id,
        kind="take",
        actor="owner",
        reason=None,
        role_path="",
        overlay_ref=role.overlay_ref,
    )


def _release(roles: SqlAlchemyRoleStore, role: Role) -> Role | None:
    return roles.release(role, actor="owner", reason="test", role_path="")


def _line_up(store: SqlAlchemyRoleStore, parties: int) -> None:
    """Hold every writer before its transaction until all of them have read the row."""
    barrier = threading.Barrier(parties, timeout=15)
    store.before_write_hook = lambda _operation: barrier.wait() and None


def _run_all(*calls: Callable[[], object]) -> list[object]:
    """Run each call on its own thread; a raised ``OmnigentError`` becomes its result."""
    results: list[object] = [None] * len(calls)

    def run(index: int, call: Callable[[], object]) -> None:
        try:
            results[index] = call()
        except OmnigentError as error:
            results[index] = error

    threads = [
        threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return results


def test_create_folds_case_and_keeps_the_display_name(roles: SqlAlchemyRoleStore) -> None:
    role = roles.create("Architect", user_id=OWNER, project_id=None)

    assert (role.name, role.name_key, role.generation) == ("Architect", "architect", 0)
    assert roles.find("ARCHITECT", user_id=OWNER, project_id=None) == role
    assert roles.get(role.id, user_id=OWNER) == role


@pytest.mark.parametrize("user_id", ["alice", None], ids=["owned", "single-user"])
def test_name_is_unique_per_owner_and_namespace(
    roles: SqlAlchemyRoleStore, db_uri: str, user_id: str | None
) -> None:
    """The sentinel keys make the index hold for NULL owners and NULL projects."""
    roles.create("Bench", user_id=user_id, project_id=None)
    with pytest.raises(OmnigentError) as caught:
        roles.create("bench", user_id=user_id, project_id=None)
    assert caught.value.code == ErrorCode.ALREADY_EXISTS

    project = _id()
    SqlAlchemyProjectStore(db_uri).create(project, "ns", user_id)
    assert roles.create("Bench", user_id=user_id, project_id=project).project_id == project
    assert roles.create("Bench", user_id="bob", project_id=None).user_id == "bob"


def test_owner_scopes_every_read(roles: SqlAlchemyRoleStore) -> None:
    role = roles.create("Bench", user_id=OWNER, project_id=None)

    assert roles.get(role.id, user_id="bob") is None
    assert roles.find("Bench", user_id="bob", project_id=None) is None
    assert roles.list_namespace(user_id="bob", project_id=None) == []
    assert roles.list_owned(user_id="bob") == []


def test_overlay_equal_to_the_name_stores_nothing(roles: SqlAlchemyRoleStore) -> None:
    assert (
        roles.create("Bench", user_id=OWNER, project_id=None, overlay_ref="bench").overlay_ref
        is None
    )
    assert (
        roles.create("Other", user_id=OWNER, project_id=None, overlay_ref="Bench").overlay_ref
        == "Bench"
    )


def test_take_and_release_bump_the_generation_and_write_events(
    roles: SqlAlchemyRoleStore,
) -> None:
    first, second = _id(), _id()
    role = roles.create("Bench", user_id=OWNER, project_id=None)

    held = _take(roles, role, first)
    assert held is not None
    assert (held.holder_session_id, held.generation) == (first, 1)
    assert held.held_since is not None
    vacant = _release(roles, held)
    assert vacant is not None
    assert (vacant.holder_session_id, vacant.previous_session_id) == (None, first)
    assert (vacant.generation, vacant.held_since) == (2, None)
    # A take from vacant keeps naming the last holder.
    again = _take(roles, vacant, second)
    assert again is not None and again.previous_session_id == first

    events = roles.events(role.id)
    assert [(e.kind, e.generation, e.session_id) for e in events] == [
        ("take", 1, first),
        ("release", 2, first),
        ("take", 3, second),
    ]
    assert {(e.role_name, e.role_path, e.actor) for e in events} == {("Bench", "", "owner")}


def test_take_on_a_stale_generation_writes_nothing(roles: SqlAlchemyRoleStore) -> None:
    role = roles.create("Bench", user_id=OWNER, project_id=None)
    winner = _take(roles, role, _id())

    assert _take(roles, role, _id()) is None

    assert roles.get(role.id, user_id=OWNER) == winner
    assert len(roles.events(role.id)) == 1


def test_a_session_holds_one_role_and_vacant_rows_never_collide(
    roles: SqlAlchemyRoleStore,
) -> None:
    holder = _id()
    first = roles.create("One", user_id=OWNER, project_id=None)
    second = roles.create("Two", user_id=OWNER, project_id=None)
    assert roles.create("Three", user_id=OWNER, project_id=None).holder_session_id is None
    assert _take(roles, first, holder) is not None

    with pytest.raises(OmnigentError) as caught:
        _take(roles, second, holder)

    assert caught.value.code == ErrorCode.CONFLICT
    assert roles.get(second.id, user_id=OWNER).generation == 0
    assert roles.events(second.id) == []


@pytest.mark.parametrize("operation", ["take", "release"])
def test_event_failure_rolls_the_cas_back(roles: SqlAlchemyRoleStore, operation: str) -> None:
    role = roles.create("Bench", user_id=OWNER, project_id=None)
    if operation == "release":
        role = _take(roles, role, _id())

    def fail(_kind: str) -> None:
        raise RuntimeError("event write failed")

    roles.before_event_hook = fail
    with pytest.raises(RuntimeError):
        if operation == "take":
            _take(roles, role, _id())
        else:
            _release(roles, role)
    roles.before_event_hook = None

    assert roles.get(role.id, user_id=OWNER) == role
    assert len(roles.events(role.id)) == role.generation


def test_adopting_create_writes_the_holder_and_event_together(
    roles: SqlAlchemyRoleStore,
) -> None:
    holder = _id()
    role = roles.create(
        "Bench",
        user_id=OWNER,
        project_id=None,
        holder_session_id=holder,
        actor="owner",
        reason="label",
        role_path="",
    )

    assert (role.holder_session_id, role.generation) == (holder, 1)
    event = roles.events(role.id)[0]
    assert (event.kind, event.reason, event.generation) == ("adopt", "label", 1)


def test_two_takes_at_one_generation_leave_one_winner(roles: SqlAlchemyRoleStore) -> None:
    """Exactly one CAS lands, and only it leaves an event."""
    role = roles.create("Bench", user_id=OWNER, project_id=None)
    first, second = _id(), _id()
    _line_up(roles, 2)

    outcomes = _run_all(lambda: _take(roles, role, first), lambda: _take(roles, role, second))

    roles.before_write_hook = None
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    current = roles.get(role.id, user_id=OWNER)
    assert current.generation == 1
    assert current.holder_session_id == winners[0].holder_session_id
    assert [event.session_id for event in roles.events(role.id)] == [current.holder_session_id]


def test_two_creates_of_one_name_leave_one_row(roles: SqlAlchemyRoleStore) -> None:
    _line_up(roles, 2)

    outcomes = _run_all(
        lambda: roles.create("Bench", user_id=OWNER, project_id=None),
        lambda: roles.create("BENCH", user_id=OWNER, project_id=None),
    )

    roles.before_write_hook = None
    assert len(roles.list_owned(user_id=OWNER)) == 1
    errors = [outcome for outcome in outcomes if isinstance(outcome, OmnigentError)]
    assert [error.code for error in errors] == [ErrorCode.ALREADY_EXISTS]


def test_one_session_racing_for_two_roles_gets_one(roles: SqlAlchemyRoleStore) -> None:
    first = roles.create("One", user_id=OWNER, project_id=None)
    second = roles.create("Two", user_id=OWNER, project_id=None)
    holder = _id()
    _line_up(roles, 2)

    outcomes = _run_all(lambda: _take(roles, first, holder), lambda: _take(roles, second, holder))

    roles.before_write_hook = None
    assert [role.id for role in roles.held_by([holder])] in ([first.id], [second.id])
    errors = [outcome for outcome in outcomes if isinstance(outcome, OmnigentError)]
    assert [error.code for error in errors] == [ErrorCode.CONFLICT]
    total_events = len(roles.events(first.id)) + len(roles.events(second.id))
    assert total_events == 1


def test_release_loses_to_a_take_at_its_generation(roles: SqlAlchemyRoleStore) -> None:
    """A release decided before a repair must not vacate the new holder's tenure."""
    role = roles.create("Bench", user_id=OWNER, project_id=None)
    held = _take(roles, role, _id())
    repaired = _take(roles, held, _id())

    assert _release(roles, held) is None

    assert roles.get(role.id, user_id=OWNER) == repaired


def test_take_in_a_deleted_project_is_refused(roles: SqlAlchemyRoleStore, db_uri: str) -> None:
    """The take re-checks under a lock, so a delete it raced cannot strand a holder."""
    projects = SqlAlchemyProjectStore(db_uri)
    project = _id()
    projects.create(project, "ns", OWNER)
    role = roles.create("Bench", user_id=OWNER, project_id=project)
    assert projects.delete(project, user_id=OWNER)

    with pytest.raises(OmnigentError) as caught:
        _take(roles, role, _id())

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert roles.get(role.id, user_id=OWNER).holder_session_id is None
    assert roles.events(role.id) == []


def test_project_delete_waits_for_a_take_holding_the_project_lock(
    roles: SqlAlchemyRoleStore, db_uri: str
) -> None:
    """Row locks on a server, or writer order on SQLite: the take is never lost."""
    projects = SqlAlchemyProjectStore(db_uri)
    project = _id()
    projects.create(project, "ns", OWNER)
    role = roles.create("Bench", user_id=OWNER, project_id=project)
    reached, resume = threading.Event(), threading.Event()

    def hold_take(_kind: str) -> None:
        reached.set()
        assert resume.wait(timeout=15)

    roles.before_event_hook = hold_take
    holder = _id()
    outcomes = {}
    taker = threading.Thread(
        target=lambda: outcomes.update(taken=_take(roles, role, holder)), name="taker"
    )
    deleter = threading.Thread(
        target=lambda: outcomes.update(deleted=projects.delete(project, user_id=OWNER)),
        name="deleter",
    )
    taker.start()
    assert reached.wait(timeout=15)
    deleter.start()
    deleter.join(timeout=1)
    assert deleter.is_alive(), "the delete must wait for the take's share lock"
    resume.set()
    taker.join(timeout=15)
    deleter.join(timeout=15)

    assert outcomes["taken"] is not None and outcomes["deleted"] is True
    assert roles.get(role.id, user_id=OWNER).holder_session_id == holder

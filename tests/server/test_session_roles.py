"""Role take, release, reconcile and resolution over real stores.

Labels and rows are real, so write order and repair are asserted on what both
stores end up holding. Runs on SQLite and on ``OMNIGENT_TEST_DB_URI``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.session_aliases import (
    Namespace,
    claim_alias_for_new_session,
    clear_session_alias,
    namespace_for_project,
    release_project_roles,
    release_role_after_lifecycle,
    release_session_role,
    resolve_session_address,
    role_handle,
    session_handle,
    set_session_alias,
    source_namespace,
    take_role,
    workspace_namespace,
)
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ROLE_ID_LABEL_KEY,
    ROLE_OVERLAY_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

OWNER = "alice"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    projects = SqlAlchemyProjectStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(OWNER)
    return SimpleNamespace(
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        projects=projects,
        roles=projects.roles,
    )


def _session(stores: SimpleNamespace, project_id: str | None = None, title: str = "s") -> str:
    conversation = stores.conversations.create_conversation(title=title, agent_id=AGENT_ID)
    stores.permissions.grant(OWNER, conversation.id, LEVEL_OWNER)
    if project_id is not None:
        assert stores.conversations.set_conversation_project(conversation.id, project_id)
    return conversation.id


def _project(
    stores: SimpleNamespace, name: str, parent_id: str | None = None, **config: str
) -> str:
    project_id = uuid.uuid4().hex
    stores.projects.create(project_id, name, OWNER, config=config or None, parent_id=parent_id)
    return project_id


def _get(stores: SimpleNamespace, session_id: str):
    return stores.conversations.get_conversation(session_id)


def _labels(stores: SimpleNamespace, session_id: str) -> dict[str, str]:
    return _get(stores, session_id).labels


def _put(stores: SimpleNamespace, session_id: str, alias: str) -> str:
    return set_session_alias(
        stores.conversations,
        stores.projects,
        session=_get(stores, session_id),
        user_id=OWNER,
        alias=alias,
    )


def _resolve(stores: SimpleNamespace, address: str, **kwargs: object) -> str:
    kwargs.setdefault("source", None)
    return resolve_session_address(
        stores.conversations, stores.projects, address=address, user_id=OWNER, **kwargs
    )


def _role(stores: SimpleNamespace, name: str, project_id: str | None = None):
    return stores.roles.find(name, user_id=OWNER, project_id=project_id)


def _kinds(stores: SimpleNamespace, role_id: str) -> list[tuple[str, int]]:
    return [(event.kind, event.generation) for event in stores.roles.events(role_id)]


def _archive(stores: SimpleNamespace, session_id: str) -> None:
    stores.conversations.update_conversation_with_changes(session_id, archived=True)


# ── take ─────────────────────────────────────────────────────────────────


def test_put_alias_creates_the_row_and_mirrors_it(stores: SimpleNamespace) -> None:
    session = _session(stores)

    assert _put(stores, session, "Architect") == "Architect"

    role = _role(stores, "architect")
    assert (role.holder_session_id, role.generation) == (session, 1)
    labels = _labels(stores, session)
    assert labels[ALIAS_LABEL_KEY] == "Architect"
    assert labels[ROLE_ID_LABEL_KEY] == role.id
    assert ROLE_OVERLAY_LABEL_KEY not in labels
    assert _kinds(stores, role.id) == [("take", 1)]


def test_put_alias_is_idempotent_and_repairs_the_holders_labels(stores: SimpleNamespace) -> None:
    session = _session(stores)
    _put(stores, session, "Architect")
    stores.conversations.delete_label(session, ROLE_ID_LABEL_KEY)

    assert _put(stores, session, "ARCHITECT") == "Architect"

    role = _role(stores, "Architect")
    assert role.generation == 1
    assert _labels(stores, session)[ROLE_ID_LABEL_KEY] == role.id


def test_a_vacant_row_is_reused_with_a_stable_id(stores: SimpleNamespace) -> None:
    first, second = _session(stores), _session(stores)
    _put(stores, first, "Bench")
    role_id = _role(stores, "Bench").id
    assert release_session_role(stores.conversations, stores.projects, first) is not None

    assert _put(stores, second, "bench") == "Bench"

    role = _role(stores, "Bench")
    assert (role.id, role.holder_session_id, role.generation) == (role_id, second, 3)
    assert role.previous_session_id == first
    assert _kinds(stores, role_id) == [("take", 1), ("release", 2), ("take", 3)]


def test_a_second_role_for_a_session_is_rejected(stores: SimpleNamespace) -> None:
    session = _session(stores)
    _put(stores, session, "One")

    with pytest.raises(OmnigentError) as caught:
        _put(stores, session, "Two")

    assert caught.value.code == ErrorCode.CONFLICT
    assert "already holds role 'One'" in caught.value.message
    assert _role(stores, "Two") is None
    assert _labels(stores, session)[ALIAS_LABEL_KEY] == "One"


def test_a_live_holder_blocks_a_take_with_its_id(stores: SimpleNamespace) -> None:
    holder, rival = _session(stores), _session(stores)
    _put(stores, holder, "Bench")

    with pytest.raises(OmnigentError) as caught:
        _put(stores, rival, "bench")

    assert caught.value.code == ErrorCode.CONFLICT
    assert holder in caught.value.message
    assert ALIAS_LABEL_KEY not in _labels(stores, rival)


@pytest.mark.parametrize(
    "end_holder",
    [
        lambda stores, session: _archive(stores, session),
        lambda stores, session: stores.conversations.set_labels(
            session, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}
        ),
    ],
    ids=["archived", "closed"],
)
def test_a_stale_holder_counts_as_vacant_and_loses_its_labels(
    stores: SimpleNamespace, end_holder
) -> None:
    """A release hook that never ran (crash, older build) is repaired by the next take."""
    stale, fresh = _session(stores), _session(stores)
    _put(stores, stale, "Bench")
    end_holder(stores, stale)

    assert _put(stores, fresh, "Bench") == "Bench"

    role = _role(stores, "Bench")
    assert (role.holder_session_id, role.previous_session_id) == (fresh, stale)
    assert _kinds(stores, role.id) == [("take", 1), ("stale_repair", 2)]
    assert not {ALIAS_LABEL_KEY, ROLE_ID_LABEL_KEY} & set(_labels(stores, stale))


def test_a_deleted_holder_counts_as_vacant(stores: SimpleNamespace) -> None:
    gone, fresh = _session(stores), _session(stores)
    _put(stores, gone, "Bench")

    assert asyncio.run(stores.conversations.delete_conversation(gone))

    assert _put(stores, fresh, "Bench") == "Bench"
    assert _kinds(stores, _role(stores, "Bench").id)[-1] == ("stale_repair", 2)


def test_take_refuses_a_child_and_an_archived_session(stores: SimpleNamespace) -> None:
    parent = _session(stores)
    child = stores.conversations.create_conversation(
        title="c", agent_id=AGENT_ID, parent_conversation_id=parent
    )
    archived = _session(stores)
    _archive(stores, archived)

    for session_id in (child.id, archived):
        with pytest.raises(OmnigentError) as caught:
            _put(stores, session_id, "Bench")
        assert caught.value.code == ErrorCode.INVALID_INPUT
    assert _role(stores, "Bench") is None


def test_take_files_an_unfiled_session_into_the_roles_namespace(stores: SimpleNamespace) -> None:
    project = _project(stores, "ns")
    session = _session(stores)

    take_role(
        stores.conversations,
        stores.projects,
        stores.roles,
        session=_get(stores, session),
        namespace=namespace_for_project(project, stores.projects, OWNER),
        user_id=OWNER,
        name="Bench",
    )

    assert _get(stores, session).project_id == project
    assert _role(stores, "Bench", project).holder_session_id == session


def test_take_refuses_a_session_filed_elsewhere(
    stores: SimpleNamespace,
) -> None:
    here, there = _project(stores, "here"), _project(stores, "there")
    session = _session(stores, here)

    with pytest.raises(OmnigentError) as caught:
        take_role(
            stores.conversations,
            stores.projects,
            stores.roles,
            session=_get(stores, session),
            namespace=namespace_for_project(there, stores.projects, OWNER),
            user_id=OWNER,
            name="Bench",
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert caught.value.message == (
        f"Session {session} is filed in here; file it in there first "
        f"(PATCH /v1/sessions/{session} with that project's id)"
    )


def test_put_adopts_the_sessions_own_label_only(stores: SimpleNamespace) -> None:
    labelled, rival = _session(stores), _session(stores)
    stores.conversations.set_labels(labelled, {ALIAS_LABEL_KEY: "Bench"})

    # Another session's label blocks, and the scan writes nothing.
    with pytest.raises(OmnigentError) as caught:
        _put(stores, rival, "Bench")
    assert caught.value.code == ErrorCode.CONFLICT
    assert _role(stores, "Bench") is None

    assert _put(stores, labelled, "Bench") == "Bench"
    role = _role(stores, "Bench")
    assert role.holder_session_id == labelled
    event = stores.roles.events(role.id)[0]
    assert (event.kind, event.reason, event.generation) == ("adopt", "label", 1)


def test_multi_holder_label_fallback_is_a_conflict_on_every_write(
    stores: SimpleNamespace,
) -> None:
    first, second, rival = _session(stores), _session(stores), _session(stores)
    for session in (first, second):
        stores.conversations.set_labels(session, {ALIAS_LABEL_KEY: "Bench"})

    for session in (first, rival):
        with pytest.raises(OmnigentError) as caught:
            _put(stores, session, "Bench")
        assert caught.value.code == ErrorCode.CONFLICT
        assert first in caught.value.message and second in caught.value.message
    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "Bench")
    assert caught.value.code == ErrorCode.CONFLICT
    assert _role(stores, "Bench") is None


def test_label_project_sessions_keep_plain_label_aliases(stores: SimpleNamespace) -> None:
    """Legacy label-projects cannot have rows; only ``roles adopt`` promotes them."""
    session = _session(stores)
    stores.conversations.set_labels(session, {PROJECT_LABEL_KEY: "legacy"})

    assert _put(stores, session, "Bench") == "Bench"

    assert stores.roles.list_owned(user_id=OWNER) == []
    assert _resolve(stores, "legacy/Bench") == session
    clear_session_alias(stores.conversations, stores.projects, session)
    assert ALIAS_LABEL_KEY not in _labels(stores, session)


# ── create then claim ────────────────────────────────────────────────────


def test_claim_writes_the_overlay_ref_and_a_reused_row_keeps_it(stores: SimpleNamespace) -> None:
    first, second = _session(stores), _session(stores)
    claim = {"user_id": OWNER, "alias": "reviewer-7", "overlay_ref": "Bench", "actor": "agent"}

    claim_alias_for_new_session(stores.conversations, stores.projects, session_id=first, **claim)

    role = _role(stores, "reviewer-7")
    assert role.overlay_ref == "Bench"
    assert _labels(stores, first)[ROLE_OVERLAY_LABEL_KEY] == "Bench"
    assert stores.roles.events(role.id)[0].actor == "agent"

    release_session_role(stores.conversations, stores.projects, first)
    assert ROLE_OVERLAY_LABEL_KEY not in _labels(stores, first)
    _put(stores, second, "reviewer-7")
    assert _labels(stores, second)[ROLE_OVERLAY_LABEL_KEY] == "Bench"


def test_claim_on_a_vacant_row_replaces_the_overlay(stores: SimpleNamespace) -> None:
    first, second, third = _session(stores), _session(stores), _session(stores)
    _put(stores, first, "reviewer")
    release_session_role(stores.conversations, stores.projects, first)

    claim_alias_for_new_session(
        stores.conversations,
        stores.projects,
        session_id=second,
        user_id=OWNER,
        alias="reviewer",
        overlay_ref="Bench",
    )
    release_session_role(stores.conversations, stores.projects, second)
    claim_alias_for_new_session(
        stores.conversations,
        stores.projects,
        session_id=third,
        user_id=OWNER,
        alias="reviewer",
        overlay_ref="REVIEWER",
    )

    assert _role(stores, "reviewer").overlay_ref is None
    assert ROLE_OVERLAY_LABEL_KEY not in _labels(stores, third)


@pytest.mark.parametrize("overlay", ["../etc", "a/b", "9bad", "", 7])
def test_claim_refuses_a_malformed_overlay(stores: SimpleNamespace, overlay: object) -> None:
    session = _session(stores)

    with pytest.raises(OmnigentError) as caught:
        claim_alias_for_new_session(
            stores.conversations,
            stores.projects,
            session_id=session,
            user_id=OWNER,
            alias="Bench",
            overlay_ref=overlay,
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert _role(stores, "Bench") is None


def test_a_lost_claim_is_already_exists(stores: SimpleNamespace) -> None:
    holder, loser = _session(stores), _session(stores)
    _put(stores, holder, "Bench")

    with pytest.raises(OmnigentError) as caught:
        claim_alias_for_new_session(
            stores.conversations, stores.projects, session_id=loser, user_id=OWNER, alias="Bench"
        )

    assert caught.value.code == ErrorCode.ALREADY_EXISTS


# ── races ────────────────────────────────────────────────────────────────


def _race(stores: SimpleNamespace, *calls) -> list[object]:
    """Line two writers up after both read the row, then let them write."""
    barrier = threading.Barrier(len(calls), timeout=15)
    stores.roles.before_write_hook = lambda _operation: barrier.wait() and None
    outcomes: list[object] = [None] * len(calls)

    def run(index: int, call) -> None:
        try:
            outcomes[index] = call()
        except OmnigentError as error:
            outcomes[index] = error

    threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    stores.roles.before_write_hook = None
    return outcomes


def test_racing_takes_of_a_vacant_role_leave_one_holder_and_one_mirror(
    stores: SimpleNamespace,
) -> None:
    first, second = _session(stores), _session(stores)
    role = stores.roles.create("Bench", user_id=OWNER, project_id=None)

    outcomes = _race(
        stores, lambda: _put(stores, first, "Bench"), lambda: _put(stores, second, "Bench")
    )

    errors = [outcome for outcome in outcomes if isinstance(outcome, OmnigentError)]
    assert [error.code for error in errors] == [ErrorCode.CONFLICT]
    current = stores.roles.get(role.id, user_id=OWNER)
    assert current.generation == 1
    labelled = [s for s in (first, second) if ALIAS_LABEL_KEY in _labels(stores, s)]
    assert labelled == [current.holder_session_id]
    assert errors[0].message.endswith(current.holder_session_id)
    assert _kinds(stores, role.id) == [("take", 1)]


def test_racing_creates_of_a_new_name_leave_one_row_and_one_holder(
    stores: SimpleNamespace,
) -> None:
    first, second = _session(stores), _session(stores)

    # The rows are created by both, so only the row insert and the CAS decide.
    outcomes = _race(
        stores, lambda: _put(stores, first, "Bench"), lambda: _put(stores, second, "bench")
    )

    assert len(stores.roles.list_owned(user_id=OWNER)) == 1
    holder = _role(stores, "Bench").holder_session_id
    assert holder in (first, second)
    errors = [outcome for outcome in outcomes if isinstance(outcome, OmnigentError)]
    assert [error.code for error in errors] == [ErrorCode.CONFLICT]


def test_a_stale_holders_release_cannot_vacate_the_repaired_tenure(
    stores: SimpleNamespace,
) -> None:
    """A release that read the row before a stale repair stands down when its CAS loses."""
    stale, fresh = _session(stores), _session(stores)
    _put(stores, stale, "Bench")
    _archive(stores, stale)
    reached, resume = threading.Event(), threading.Event()

    def hold_release(operation: str) -> None:
        if operation == "release" and threading.current_thread().name == "releaser":
            reached.set()
            assert resume.wait(timeout=15)

    stores.roles.before_write_hook = hold_release
    outcome: dict[str, object] = {}
    releaser = threading.Thread(
        name="releaser",
        target=lambda: outcome.update(
            released=release_session_role(stores.conversations, stores.projects, stale)
        ),
    )
    releaser.start()
    assert reached.wait(timeout=15)
    _put(stores, fresh, "Bench")
    resume.set()
    releaser.join(timeout=15)
    stores.roles.before_write_hook = None

    assert outcome["released"] is None
    assert _role(stores, "Bench").holder_session_id == fresh
    assert _kinds(stores, _role(stores, "Bench").id) == [("take", 1), ("stale_repair", 2)]


def test_a_take_racing_a_project_delete_is_refused_after_the_sweep(
    stores: SimpleNamespace,
) -> None:
    """The delete's post-commit release ran already, so the take must not land."""
    project = _project(stores, "doomed")
    holder = _session(stores, project)
    role = stores.roles.create("Bench", user_id=OWNER, project_id=project)
    reached, resume = threading.Event(), threading.Event()

    def hold_take(operation: str) -> None:
        if operation == "take":
            reached.set()
            assert resume.wait(timeout=15)

    stores.roles.before_write_hook = hold_take
    outcome: dict[str, object] = {}

    def take() -> None:
        try:
            outcome["taken"] = _put(stores, holder, "Bench")
        except OmnigentError as error:
            outcome["error"] = error

    taker = threading.Thread(target=take)
    taker.start()
    assert reached.wait(timeout=15)
    sweep = stores.projects.delete_with_sessions(
        project,
        user_id=OWNER,
        conversation_store=stores.conversations,
        authorize=lambda _ids: None,
    )
    release_project_roles(
        stores.conversations,
        stores.projects,
        project,
        session_ids=sweep.session_ids,
        user_id=OWNER,
        role_path="doomed",
    )
    resume.set()
    taker.join(timeout=15)
    stores.roles.before_write_hook = None

    assert outcome["error"].code == ErrorCode.NOT_FOUND
    assert stores.roles.get(role.id, user_id=OWNER).holder_session_id is None


# ── write order and reconcile ────────────────────────────────────────────


class _FailingLabels:
    """Fail the Nth matching label write, like a crash between two stores."""

    def __init__(self, conversations, method: str) -> None:
        self.real = getattr(conversations, method)
        self.failing = True

    def __call__(self, *args: object, **kwargs: object) -> object:
        if self.failing:
            raise RuntimeError("label store unreachable")
        return self.real(*args, **kwargs)


def test_crash_between_row_and_labels_is_repaired_by_the_next_resolve(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A take writes the row first, so the crash leaves it held and the row wins."""
    session = _session(stores)
    failing = _FailingLabels(stores.conversations, "set_labels")
    monkeypatch.setattr(stores.conversations, "set_labels", failing)

    with pytest.raises(RuntimeError):
        _put(stores, session, "Bench")

    role = _role(stores, "Bench")
    assert role.holder_session_id == session
    assert ALIAS_LABEL_KEY not in _labels(stores, session)
    failing.failing = False

    assert _resolve(stores, "/Bench") == session

    labels = _labels(stores, session)
    assert (labels[ALIAS_LABEL_KEY], labels[ROLE_ID_LABEL_KEY]) == ("Bench", role.id)
    assert _role(stores, "Bench").generation == role.generation


def test_a_release_that_fails_on_the_labels_leaves_the_role_held(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")
    failing = _FailingLabels(stores.conversations, "delete_label")
    monkeypatch.setattr(stores.conversations, "delete_label", failing)

    with pytest.raises(RuntimeError):
        clear_session_alias(stores.conversations, stores.projects, session)

    assert _role(stores, "Bench").holder_session_id == session
    failing.failing = False
    clear_session_alias(stores.conversations, stores.projects, session)
    assert _role(stores, "Bench").holder_session_id is None


def test_a_crash_between_labels_and_row_is_an_error_and_the_label_comes_back(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels go first: the row stays held, the caller sees the error, reconcile restores."""
    session = _session(stores)
    _put(stores, session, "Bench")
    role = _role(stores, "Bench")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("row write failed")

    with monkeypatch.context() as patch:
        patch.setattr(stores.roles, "release", fail)
        with pytest.raises(RuntimeError):
            clear_session_alias(stores.conversations, stores.projects, session)
    assert ALIAS_LABEL_KEY not in _labels(stores, session)

    assert _resolve(stores, "/Bench") == session

    assert _labels(stores, session)[ALIAS_LABEL_KEY] == "Bench"
    assert _role(stores, "Bench") == role


def test_lifecycle_release_retries_then_logs_without_raising(
    stores: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")
    calls: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> None:
        calls.append("release")
        raise RuntimeError("row write failed")

    monkeypatch.setattr(stores.roles, "release", fail)

    release_role_after_lifecycle(
        stores.conversations, stores.projects, session, reason="close", sleep_seconds=0
    )

    assert len(calls) == 3
    assert "Releasing the role" in caplog.text


def test_lifecycle_release_recovers_on_a_retry(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")
    real = stores.roles.release
    calls: list[str] = []

    def flaky(*args: object, **kwargs: object) -> object:
        calls.append("release")
        if len(calls) == 1:
            raise RuntimeError("row write failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(stores.roles, "release", flaky)

    release_role_after_lifecycle(
        stores.conversations, stores.projects, session, reason="archive", sleep_seconds=0
    )

    assert _role(stores, "Bench").holder_session_id is None
    assert stores.roles.events(_role(stores, "Bench").id)[-1].reason == "archive"


def test_reconcile_strips_labels_from_a_session_that_is_not_the_holder(
    stores: SimpleNamespace,
) -> None:
    holder, impostor = _session(stores), _session(stores)
    _put(stores, holder, "Bench")
    stores.conversations.set_labels(impostor, {ALIAS_LABEL_KEY: "bench"})

    assert _resolve(stores, "Bench") == holder

    assert ALIAS_LABEL_KEY not in _labels(stores, impostor)


def test_a_failed_repair_does_not_fail_the_resolve(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = _session(stores)
    _put(stores, holder, "Bench")
    stores.conversations.delete_label(holder, ALIAS_LABEL_KEY)
    monkeypatch.setattr(
        stores.conversations, "set_labels", _FailingLabels(stores.conversations, "set_labels")
    )

    assert _resolve(stores, "Bench") == holder


def test_resolve_never_adopts_after_a_release(stores: SimpleNamespace) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")
    role = _role(stores, "Bench")
    release_session_role(stores.conversations, stores.projects, session)
    # A stale label must not bring the released holder back.
    stores.conversations.set_labels(session, {ALIAS_LABEL_KEY: "Bench"})

    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "Bench")

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert caught.value.message == (
        "Role 'Bench' in namespace '/' is vacant. Use sys_role_list to see the roles here."
    )
    assert ALIAS_LABEL_KEY not in _labels(stores, session)
    assert stores.roles.get(role.id, user_id=OWNER).holder_session_id is None


def test_resolve_never_creates_a_row(stores: SimpleNamespace) -> None:
    session = _session(stores)
    stores.conversations.set_labels(session, {ALIAS_LABEL_KEY: "Bench"})

    assert _resolve(stores, "Bench") == session

    assert stores.roles.list_owned(user_id=OWNER) == []


def test_resolve_treats_a_dead_holder_as_vacant_without_touching_the_row(
    stores: SimpleNamespace,
) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")
    role = _role(stores, "Bench")
    _archive(stores, session)

    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "Bench")

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert _role(stores, "Bench") == role


# ── release ──────────────────────────────────────────────────────────────


def test_release_is_idempotent_and_keeps_the_row(stores: SimpleNamespace) -> None:
    session = _session(stores)
    _put(stores, session, "Bench")

    released = release_session_role(
        stores.conversations, stores.projects, session, actor="owner", reason="alias_cleared"
    )

    assert released is not None and released.generation == 2
    assert not {ALIAS_LABEL_KEY, ROLE_ID_LABEL_KEY} & set(_labels(stores, session))
    assert release_session_role(stores.conversations, stores.projects, session) is None
    assert _kinds(stores, released.id) == [("take", 1), ("release", 2)]
    assert _role(stores, "Bench").previous_session_id == session


def test_release_of_a_session_without_a_role_changes_nothing(stores: SimpleNamespace) -> None:
    holder, bystander = _session(stores), _session(stores)
    _put(stores, holder, "Bench")
    stores.conversations.set_labels(bystander, {ALIAS_LABEL_KEY: "Other"})

    assert release_session_role(stores.conversations, stores.projects, bystander) is None

    assert _labels(stores, bystander)[ALIAS_LABEL_KEY] == "Other"
    assert _role(stores, "Bench").holder_session_id == holder


def test_project_sweep_releases_with_the_snapshotted_path(stores: SimpleNamespace) -> None:
    project = _project(stores, "doomed")
    holder = _session(stores, project)
    _put(stores, holder, "Bench")
    role = _role(stores, "Bench", project)
    sweep = stores.projects.delete_with_sessions(
        project,
        user_id=OWNER,
        conversation_store=stores.conversations,
        authorize=lambda _ids: None,
    )

    release_project_roles(
        stores.conversations,
        stores.projects,
        project,
        session_ids=sweep.session_ids,
        user_id=OWNER,
        role_path="doomed",
    )

    assert stores.roles.get(role.id, user_id=OWNER).holder_session_id is None
    event = stores.roles.events(role.id)[-1]
    assert (event.kind, event.reason, event.role_path) == ("release", "project_delete", "doomed")
    assert ROLE_ID_LABEL_KEY not in _labels(stores, holder)


# ── resolution ───────────────────────────────────────────────────────────


def test_bare_role_follows_each_callers_source_namespace(stores: SimpleNamespace) -> None:
    """U1: agent → its project; CLI → the cwd's project; web → the project in view."""
    alpha = _project(stores, "alpha", workspace="/srv/alpha")
    beta = _project(stores, "beta", workspace="/srv/beta")
    in_alpha, in_beta, at_root = (
        _session(stores, alpha),
        _session(stores, beta),
        _session(stores),
    )
    for session in (in_alpha, in_beta, at_root):
        _put(stores, session, "Bench")

    def bare(**caller: object) -> str:
        namespace = source_namespace(
            stores.conversations, stores.projects, user_id=OWNER, **caller
        )
        return _resolve(stores, "Bench", default_namespace=namespace)

    assert bare(source=_get(stores, in_beta)) == in_beta
    assert bare(view="alpha") == in_alpha
    assert bare(view="/") == at_root
    assert bare(workspace="/srv/beta") == in_beta
    assert bare(workspace="/srv/beta/deep/down") == in_beta
    assert bare(workspace="/srv/other") == at_root
    assert bare() == at_root
    # The agent's own project outranks anything a caller could pass.
    assert bare(source=_get(stores, in_alpha), view="beta", workspace="/srv/beta") == in_alpha


def test_workspace_match_prefers_the_deepest_project_and_refuses_a_tie(
    stores: SimpleNamespace,
) -> None:
    outer = _project(stores, "outer", workspace="/srv/repo")
    inner = _project(stores, "inner", workspace="/srv/repo/sub")

    assert workspace_namespace(stores.projects, "/srv/repo", OWNER).project_ids == {outer}
    assert workspace_namespace(stores.projects, "/srv/repo/sub/x", OWNER).project_ids == {inner}
    assert workspace_namespace(stores.projects, "/srv/repository", OWNER).path == ""
    assert workspace_namespace(None, "/srv/repo", OWNER) == Namespace("", "")

    twin = _project(stores, "twin", workspace="/srv/repo")
    with pytest.raises(OmnigentError) as caught:
        workspace_namespace(stores.projects, "/srv/repo", OWNER)
    assert caught.value.code == ErrorCode.CONFLICT
    assert outer in caught.value.message and twin in caught.value.message


def test_paths_and_role_names_match_any_case(stores: SimpleNamespace) -> None:
    parent = _project(stores, "Forth")
    child = _project(stores, "Sites", parent)
    session = _session(stores, child)
    _put(stores, session, "Bench")

    assert _resolve(stores, "forth/SITES/bench") == session


def test_case_only_siblings_are_a_conflict(stores: SimpleNamespace) -> None:
    upper, lower = _project(stores, "Dup"), _project(stores, "dup")
    _put(stores, _session(stores, upper), "Bench")

    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "DUP/Bench")

    assert caught.value.code == ErrorCode.CONFLICT
    assert upper in caught.value.message and lower in caught.value.message


def test_unfiled_roles_show_a_leading_slash_and_resolve_from_a_filed_source(
    stores: SimpleNamespace,
) -> None:
    project = _project(stores, "ns")
    filed, unfiled = _session(stores, project), _session(stores)
    _put(stores, unfiled, "Scratch")
    _put(stores, filed, "Bench")

    assert session_handle(_get(stores, unfiled), stores.projects, OWNER) == "/Scratch"
    assert session_handle(_get(stores, filed), stores.projects, OWNER) == "ns/Bench"
    assert role_handle("", "Scratch") == "/Scratch"
    assert _resolve(stores, "/Scratch", source=_get(stores, filed)) == unfiled
    with pytest.raises(OmnigentError):
        _resolve(stores, "Scratch", source=_get(stores, filed))


def test_unresolved_text_names_neighbours_and_other_namespaces(stores: SimpleNamespace) -> None:
    forthkit = _project(stores, "forthkit")
    omnigent, qwant = _project(stores, "omnigent"), _project(stores, "py-qwant")
    for role, project in (("Product", forthkit), ("Bench", forthkit)):
        _put(stores, _session(stores, project), role)
    for project in (omnigent, qwant):
        _put(stores, _session(stores, project), "Architect")

    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "forthkit/Architect")

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert caught.value.message == (
        "No role 'Architect' in namespace 'forthkit'. Roles here: Bench, Product. "
        "Other namespaces with Architect: omnigent, py-qwant. "
        "Use '<path>/Architect' or sys_role_list."
    )


def test_unresolved_text_for_the_root_with_no_neighbours(stores: SimpleNamespace) -> None:
    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "/Nobody")

    assert caught.value.message == (
        "No role 'Nobody' in namespace '/'. Roles here: none. "
        "Use '<path>/Nobody' or sys_role_list."
    )


def test_tree_and_legacy_flat_match_uses_the_exact_text(
    stores: SimpleNamespace, db_uri: str
) -> None:
    parent = _project(stores, "a")
    child = _project(stores, "b", parent)
    legacy = uuid.uuid4().hex
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(SqlProject(id=legacy, name="a/b", user_id=OWNER, created_at=1))
        session.commit()

    with pytest.raises(OmnigentError) as caught:
        _resolve(stores, "a/b/Bench")

    assert caught.value.code == ErrorCode.CONFLICT
    assert caught.value.message == (
        f"'a/b' matches both project a/b (tree, id {child}) and legacy project 'a/b' "
        f"(id {legacy}). Run `omnigent projects migrate-slashes --dry-run`."
    )

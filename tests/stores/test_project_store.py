"""Tests for :class:`SqlAlchemyProjectStore`.

Exercises ``create``, ``get``, ``list``, ``update`` and ``delete`` against a
real SQLite database, covering owner scoping and per-owner name uniqueness.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import ARCHIVED_AT_LABEL_KEY, PROJECT_LABEL_KEY
from omnigent.stores.conversation_store import sqlalchemy_store as conversation_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


# projects.id is a Uuid16 column (16 raw bytes) read back as bare 32-char hex.
# ``_uid`` maps a readable seed to a deterministic bare-hex UUID so tests stay
# legible while the store still round-trips real UUIDs.
def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyProjectStore:
    """A fresh :class:`SqlAlchemyProjectStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyProjectStore` instance.
    """
    return SqlAlchemyProjectStore(db_uri)


# ── create / get ──────────────────────────────────────────────────────────


def test_create_returns_project(store: SqlAlchemyProjectStore) -> None:
    """``create`` echoes the fields back and stamps ``created_at``."""
    project = store.create(_uid("p1"), "My Project", "alice@example.com")
    assert project.id == _uid("p1")
    assert project.name == "My Project"
    assert project.user_id == "alice@example.com"
    assert project.created_at > 0
    assert project.updated_at is None


def test_get_returns_created_project(store: SqlAlchemyProjectStore) -> None:
    """``get`` reads back a created project for its owner."""
    store.create(_uid("p1"), "My Project", "alice@example.com")
    got = store.get(_uid("p1"), user_id="alice@example.com")
    assert got is not None
    assert got.name == "My Project"


def test_get_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """``get`` returns ``None`` for an unknown id."""
    assert store.get(_uid("nope"), user_id="alice@example.com") is None


def test_get_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A project owned by someone else reads back as not found."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    assert store.get(_uid("p1"), user_id="bob@example.com") is None


# ── list ──────────────────────────────────────────────────────────────────


def test_list_orders_by_created_at_then_id(store: SqlAlchemyProjectStore) -> None:
    """``list`` orders by ``created_at ASC, id ASC``.

    Both rows are created in the same second here, so the ``id`` tiebreaker
    decides the order — assert against that rather than insertion order.
    """
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    listed = store.list(user_id="alice@example.com")
    assert {p.name for p in listed} == {"First", "Second"}
    # Whatever the tie order, it is ascending by (created_at, id).
    keys = [(p.created_at, p.id) for p in listed]
    assert keys == sorted(keys)


def test_list_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """``list`` only returns the requesting owner's projects."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    store.create(_uid("p2"), "Bob Project", "bob@example.com")
    alice = store.list(user_id="alice@example.com")
    assert [p.name for p in alice] == ["Alice Project"]


def test_list_empty(store: SqlAlchemyProjectStore) -> None:
    """``list`` returns an empty list when the owner has no projects."""
    assert store.list(user_id="alice@example.com") == []


# ── single-user (None owner) vs multi-user isolation ────────────────────────


def test_none_owner_and_named_owner_are_isolated(store: SqlAlchemyProjectStore) -> None:
    """The single-user ``None`` owner is a distinct scope from any named user.

    A project created in single-user mode (``user_id=None``) must not be
    visible to a named multi-user identity, and vice versa — the same DB can
    hold both without cross-leaking.
    """
    store.create(_uid("solo"), "Solo Project", None)
    store.create(_uid("alice"), "Alice Project", "alice@example.com")

    # Each scope lists only its own.
    assert [p.name for p in store.list(user_id=None)] == ["Solo Project"]
    assert [p.name for p in store.list(user_id="alice@example.com")] == ["Alice Project"]


def test_named_owner_cannot_get_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """A ``None``-owner project is not found for a named user (and vice versa)."""
    store.create(_uid("solo"), "Solo", None)
    assert store.get(_uid("solo"), user_id="alice@example.com") is None
    assert store.get(_uid("solo"), user_id=None) is not None


def test_named_owner_cannot_mutate_none_owner_project(store: SqlAlchemyProjectStore) -> None:
    """update / delete on a ``None``-owner project are no-ops for a named user."""
    store.create(_uid("solo"), "Solo", None)
    updated = store.update(_uid("solo"), user_id="alice@example.com", name="Hacked")
    assert updated is None
    deleted = store.delete(_uid("solo"), user_id="alice@example.com")
    assert deleted is False
    # Untouched for the real (None) owner.
    assert store.get(_uid("solo"), user_id=None).name == "Solo"


# ── name uniqueness ────────────────────────────────────────────────────────


def test_create_rejects_duplicate_name_per_owner(store: SqlAlchemyProjectStore) -> None:
    """Two projects with the same name for one owner are rejected."""
    store.create(_uid("p1"), "Dup", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Dup", "alice@example.com")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_same_name_allowed_across_owners(store: SqlAlchemyProjectStore) -> None:
    """Two different owners may each have a project with the same name."""
    a = store.create(_uid("p1"), "Shared Name", "alice@example.com")
    b = store.create(_uid("p2"), "Shared Name", "bob@example.com")
    assert a.name == b.name == "Shared Name"


def test_duplicate_name_rejected_for_null_owner(store: SqlAlchemyProjectStore) -> None:
    """Single-user mode (NULL owner) enforces name uniqueness in the store.

    ``_name_taken`` is the sole guard for every owner, NULL included — no unique
    index backs it.
    """
    store.create(_uid("p1"), "Solo", None)
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p2"), "Solo", None)
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


def test_duplicate_name_lands_when_precheck_is_bypassed(
    store: SqlAlchemyProjectStore,
) -> None:
    """A duplicate name is accepted once ``_name_taken`` is bypassed.

    No unique index covers (workspace_id, user_id, name), so the pre-check is
    the only guard and a concurrent create racing past it lands. Pins that
    accepted cost: both rows exist, each addressable by its own id.

    Monkeypatching ``_name_taken`` to always-miss simulates two concurrent
    creates both passing the check.
    """
    store.create(_uid("p1"), "Dup", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    store.create(_uid("p2"), "Dup", "alice@example.com")

    assert [p.name for p in store.list(user_id="alice@example.com")] == ["Dup", "Dup"]
    assert store.get(_uid("p1"), user_id="alice@example.com") is not None
    assert store.get(_uid("p2"), user_id="alice@example.com") is not None


def test_primary_key_collision_is_not_masked(store: SqlAlchemyProjectStore) -> None:
    """Reusing an id surfaces as ``IntegrityError``, not ``ALREADY_EXISTS``.

    The PK is the only remaining constraint on this table, and the store must
    not dress its violation up as a name collision.
    """
    store.create(_uid("p1"), "Original", "alice@example.com")
    store._name_taken = lambda *a, **k: False  # type: ignore[method-assign]
    with pytest.raises(IntegrityError):
        store.create(_uid("p1"), "Different name", "alice@example.com")


# ── config (default session settings) ──────────────────────────────────────


def test_create_defaults_to_empty_config(store: SqlAlchemyProjectStore) -> None:
    """A project created without config reads back an empty dict (SQL NULL)."""
    project = store.create(_uid("p1"), "No Config", "alice@example.com")
    assert project.config == {}
    assert store.get(_uid("p1"), user_id="alice@example.com").config == {}


def test_create_persists_config(store: SqlAlchemyProjectStore) -> None:
    """A config passed to ``create`` round-trips through ``get``/``list``."""
    cfg = {"host_id": "host_abc", "workspace": "/work/repo", "model": "claude-opus-4-8"}
    store.create(_uid("p1"), "Configured", "alice@example.com", cfg)
    assert store.get(_uid("p1"), user_id="alice@example.com").config == cfg
    assert store.list(user_id="alice@example.com")[0].config == cfg


def test_update_replaces_config_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Passing a new ``config`` replaces the stored one and stamps updated_at."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "old"})
    updated = store.update(
        _uid("p1"), user_id="alice@example.com", config={"host_id": "new", "model": "m"}
    )
    assert updated is not None
    assert updated.config == {"host_id": "new", "model": "m"}
    assert updated.updated_at is not None


def test_update_config_none_leaves_it_unchanged(store: SqlAlchemyProjectStore) -> None:
    """``config=None`` (the default) leaves the stored config untouched."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    # Rename only — config omitted — must not wipe the stored defaults.
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="Renamed")
    assert updated is not None
    assert updated.config == {"host_id": "keep"}


def test_update_empty_config_clears_defaults(store: SqlAlchemyProjectStore) -> None:
    """An explicit ``config={}`` clears the stored defaults (distinct from None)."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "drop"})
    updated = store.update(_uid("p1"), user_id="alice@example.com", config={})
    assert updated is not None
    assert updated.config == {}
    assert updated.updated_at is not None


def test_update_same_config_is_noop(store: SqlAlchemyProjectStore) -> None:
    """Re-setting the identical config changes nothing, leaving updated_at None."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "x"})
    updated = store.update(_uid("p1"), user_id="alice@example.com", config={"host_id": "x"})
    assert updated is not None
    assert updated.updated_at is None


def test_create_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """A config whose serialized form exceeds the cap is rejected on create.

    The value is persisted verbatim and reflected back on every read, so an
    unbounded blob is capped as defense-in-depth (INVALID_INPUT → HTTP 400).
    """
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.create(_uid("p1"), "Big", "alice@example.com", huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_update_rejects_oversized_config(store: SqlAlchemyProjectStore) -> None:
    """An oversized config is rejected on update, leaving the row unchanged."""
    store.create(_uid("p1"), "P", "alice@example.com", {"host_id": "keep"})
    huge = {"blob": "x" * (64 * 1024 + 1)}
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p1"), user_id="alice@example.com", config=huge)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    # The prior config is untouched (the encode guard fires before any write).
    assert store.get(_uid("p1"), user_id="alice@example.com").config == {"host_id": "keep"}


def test_decode_coerces_non_object_blob_to_empty(store: SqlAlchemyProjectStore) -> None:
    """A stored non-object blob (manual edit / future writer) reads back as {}.

    The encode path only ever writes JSON objects, but ``_decode_config`` must
    stay defensive so callers can always treat config as a mapping.
    """
    from sqlalchemy import text as sa_text

    store.create(_uid("p1"), "P", "alice@example.com")
    # Bypass the store to plant a scalar JSON blob directly.
    with store._engine.begin() as conn:
        conn.execute(
            sa_text("UPDATE projects SET config = :c WHERE id = :i"),
            {"c": '"just a string"', "i": _uid("p1")},
        )
    got = store.get(_uid("p1"), user_id="alice@example.com")
    assert got is not None
    assert got.config == {}


# ── update ─────────────────────────────────────────────────────────────────


def test_update_renames_and_stamps_updated_at(store: SqlAlchemyProjectStore) -> None:
    """Renaming changes ``name`` and sets ``updated_at``."""
    store.create(_uid("p1"), "Old", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="New")
    assert updated is not None
    assert updated.name == "New"
    assert updated.updated_at is not None


def test_update_noop_leaves_updated_at_none(store: SqlAlchemyProjectStore) -> None:
    """An update that changes nothing leaves ``updated_at`` untouched."""
    store.create(_uid("p1"), "Same", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="alice@example.com", name="Same")
    assert updated is not None
    assert updated.updated_at is None


def test_update_missing_returns_none(store: SqlAlchemyProjectStore) -> None:
    """Updating an unknown project returns ``None``."""
    updated = store.update(_uid("nope"), user_id="alice@example.com", name="X")
    assert updated is None


def test_update_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot rename another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    updated = store.update(_uid("p1"), user_id="bob@example.com", name="Hacked")
    assert updated is None
    # Unchanged for the real owner.
    assert store.get(_uid("p1"), user_id="alice@example.com").name == "Alice Project"


def test_update_rejects_duplicate_name(store: SqlAlchemyProjectStore) -> None:
    """Renaming onto another of the owner's project names is rejected."""
    store.create(_uid("p1"), "First", "alice@example.com")
    store.create(_uid("p2"), "Second", "alice@example.com")
    with pytest.raises(OmnigentError) as exc:
        store.update(_uid("p2"), user_id="alice@example.com", name="First")
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


# ── delete ─────────────────────────────────────────────────────────────────


def test_delete_removes_project(store: SqlAlchemyProjectStore) -> None:
    """``delete`` removes the project and is idempotent."""
    store.create(_uid("p1"), "Doomed", "alice@example.com")
    deleted = store.delete(_uid("p1"), user_id="alice@example.com")
    assert deleted is True
    assert store.get(_uid("p1"), user_id="alice@example.com") is None
    deleted_again = store.delete(_uid("p1"), user_id="alice@example.com")
    assert deleted_again is False


def test_delete_scoped_to_owner(store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot delete another user's project."""
    store.create(_uid("p1"), "Alice Project", "alice@example.com")
    deleted = store.delete(_uid("p1"), user_id="bob@example.com")
    assert deleted is False
    assert store.get(_uid("p1"), user_id="alice@example.com") is not None


def test_order_owner_isolation_and_last_write_wins(store: SqlAlchemyProjectStore) -> None:
    """Local and authenticated owners have independent orders and resets."""
    a = store.create(_uid("order-a"), "A", None)
    b = store.create(_uid("order-b"), "B", None)
    other = store.create(_uid("order-other"), "Other", "alice")
    assert store.get_order(user_id=None) is None
    store.save_order([b.id, a.id], user_id=None)
    store.save_order([other.id], user_id="alice")
    assert store.get_order(user_id=None) == [b.id, a.id]
    assert store.get_order(user_id="alice") == [other.id]
    store.save_order([a.id, b.id], user_id=None)
    assert store.get_order(user_id=None) == [a.id, b.id]
    for invalid in ([a.id, a.id], [other.id], ["missing"]):
        with pytest.raises(OmnigentError):
            store.save_order(invalid, user_id=None)
        assert store.get_order(user_id=None) == [a.id, b.id]
    store.save_order([], user_id=None)
    assert store.get_order(user_id=None) == []
    store.save_order(None, user_id=None)
    assert store.get_order(user_id=None) is None
    assert store.get_order(user_id="alice") == [other.id]


def test_order_workspace_isolation(store: SqlAlchemyProjectStore) -> None:
    """An owner's saved IDs and write validation stay inside their workspace."""
    from omnigent.db.db_models import workspace_scope

    with workspace_scope(101):
        project = store.create(_uid("scoped-order"), "A", "alice")
        store.save_order([project.id], user_id="alice")
    with workspace_scope(102):
        assert store.get_order(user_id="alice") is None
        with pytest.raises(OmnigentError):
            store.save_order([project.id], user_id="alice")
        store.save_order([], user_id="alice")
    with workspace_scope(101):
        assert store.get_order(user_id="alice") == [project.id]


def test_order_local_alias_does_not_change_project_ownership(
    store: SqlAlchemyProjectStore,
) -> None:
    """None and local share a preference, but keep their original project scopes."""
    project = store.create(_uid("no-auth"), "Local project", None)
    assert store.get_order(user_id="local") is None
    store.save_order([project.id], user_id=None)
    assert store.get_order(user_id="local") == [project.id]
    with pytest.raises(OmnigentError):
        store.save_order([project.id], user_id="local")
    assert store.get(project.id, user_id=None) is not None
    assert store.get(project.id, user_id="local") is None
    store.save_order(None, user_id="local")
    assert store.get_order(user_id=None) is None


@pytest.mark.parametrize("user_id", [None, "alice"])
def test_order_preserves_authentication_fields(
    store: SqlAlchemyProjectStore, user_id: str | None
) -> None:
    """Saving/resetting preferences leaves an existing account's identity intact."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlUser

    preference_user_id = "local" if user_id is None else user_id
    with Session(store._engine) as session:
        session.merge(
            SqlUser(
                workspace_id=0,
                id=preference_user_id,
                is_admin=True,
                password_hash="test-hash",
                created_at=123,
                last_login_at=456,
            )
        )
        session.commit()
    project = store.create(_uid("auth-order"), "A", user_id)
    for order in ([project.id], [], None):
        store.save_order(order, user_id=user_id)
        with Session(store._engine) as session:
            user = session.get(SqlUser, (0, preference_user_id))
            assert user is not None
            assert user.is_admin is True
            assert user.password_hash == "test-hash"
            assert user.created_at == 123
            assert user.last_login_at == 456


def test_order_preferences_do_not_create_accounts(
    store: SqlAlchemyProjectStore,
) -> None:
    """Project preferences can be saved without creating authentication rows."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, SqlUser

    with Session(store._engine) as session:
        user = session.get(SqlUser, (0, "local"))
        if user is not None:
            session.delete(user)
            session.commit()
    assert store.get_order(user_id=None) is None
    store.save_order(None, user_id=None)
    with Session(store._engine) as session:
        assert session.get(SqlUser, (0, "local")) is None
        assert session.get(SqlPreference, (0, "local", "project_order")) is None
    store.save_order([], user_id=None)
    with Session(store._engine) as session:
        assert session.get(SqlUser, (0, "local")) is None
        assert session.get(SqlPreference, (0, "local", "project_order")) is not None


def test_order_updates_preserve_other_preferences(store: SqlAlchemyProjectStore) -> None:
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference

    with Session(store._engine) as session:
        session.add(SqlPreference(workspace_id=0, user_id="local", key="theme", value='"dark"'))
        session.commit()
    assert store.get_order(user_id=None) is None
    for order in (None, [], None, []):
        store.save_order(order, user_id=None)
        with Session(store._engine) as session:
            preference = session.get(SqlPreference, (0, "local", "theme"))
            assert preference is not None
            assert preference.value == '"dark"'


def test_alphabetical_mode_retains_manual_order(store: SqlAlchemyProjectStore) -> None:
    a = store.create(_uid("remember-a"), "A", None)
    b = store.create(_uid("remember-b"), "B", None)
    store.save_order([b.id, a.id], user_id=None)
    for _ in range(2):
        preference = store.save_order(None, user_id=None)
        assert preference == {"sort_mode": "alphabetical", "ordered_project_ids": [b.id, a.id]}
        assert store.get_order(user_id=None) is None
        reopened = SqlAlchemyProjectStore(store.storage_location)
        assert reopened.get_order_preference(user_id=None) == preference
        reopened.save_order(preference["ordered_project_ids"], user_id=None)
        assert reopened.get_order(user_id=None) == [b.id, a.id]


def test_original_array_format_preserves_manual_order(store: SqlAlchemyProjectStore) -> None:
    import json

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference

    project = store.create(_uid("original-format"), "A", None)
    store.save_order([], user_id=None)
    with Session(store._engine) as session:
        session.execute(
            update(SqlPreference)
            .where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "local",
                SqlPreference.key == "project_order",
            )
            .values(value=json.dumps([project.id]))
        )
        session.commit()
    assert store.get_order(user_id=None) == [project.id]
    assert store.save_order(None, user_id=None) == {
        "sort_mode": "alphabetical",
        "ordered_project_ids": [project.id],
    }


def test_concurrent_order_saves_and_mode_changes_retain_manual_ids(
    store: SqlAlchemyProjectStore,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    a = store.create(_uid("concurrent-order-a"), "A", "new-owner")
    b = store.create(_uid("concurrent-order-b"), "B", "new-owner")
    orders = [[a.id, b.id], [b.id, a.id]]
    barrier = Barrier(2)

    def save(ids: list[str]) -> None:
        barrier.wait(timeout=5)
        for _ in range(3):
            store.save_order(ids, user_id="new-owner")
            store.save_order(None, user_id="new-owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, orders))
    preference = store.get_order_preference(user_id="new-owner")
    assert preference["sort_mode"] == "alphabetical"
    assert preference["ordered_project_ids"] in orders


def test_order_enforces_compressed_blob_capacity(store: SqlAlchemyProjectStore) -> None:
    """Limit stored bytes while retaining orders whose larger JSON compresses to fit."""
    import json

    from sqlalchemy import func, insert, select
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, SqlProject
    from omnigent.server.schemas import ProjectOrderRequest

    ids = [_uid(f"large-order-{i}") for i in range(10000)]
    with Session(store._engine) as session:
        session.execute(
            insert(SqlProject),
            [
                {"workspace_id": 0, "id": id, "name": id, "user_id": "large", "created_at": 0}
                for id in ids
            ],
        )
        session.commit()
    request = ProjectOrderRequest(ordered_project_ids=ids)
    with pytest.raises(OmnigentError, match="too large") as exc:
        store.save_order(request.ordered_project_ids, user_id="large")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert store.get_order(user_id="large") is None

    fitting_ids = ids[:3000]
    assert len(json.dumps(fitting_ids).encode()) > 65535
    store.save_order(fitting_ids, user_id="large")
    assert store.get_order(user_id="large") == fitting_ids
    with Session(store._engine) as session:
        size = session.scalar(
            select(func.length(SqlPreference.value)).where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "large",
                SqlPreference.key == "project_order",
            )
        )
    assert size is not None and size <= 65535

    with pytest.raises(OmnigentError, match="too large") as exc:
        store.save_order(request.ordered_project_ids, user_id="large")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert store.get_order(user_id="large") == fitting_ids
    assert store.save_order(None, user_id="large") == {
        "sort_mode": "alphabetical",
        "ordered_project_ids": fitting_ids,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"null",
        b"\xff",
        b"\x00",
        b"\x00\x01invalid",
        b"\x00\x03{}",
        b'{"sort_mode":"invalid","ordered_project_ids":[]}',
        b'{"sort_mode":"manual","ordered_project_ids":[5]}',
        b'{"sort_mode":"manual","ordered_project_ids":["invalid"]}',
        b'{"sort_mode":"manual","ordered_project_ids":{}}',
        b'{"sort_mode":"manual","ordered_project_ids":null}',
    ],
)
def test_invalid_order_falls_back_and_can_be_replaced(
    store: SqlAlchemyProjectStore, raw: bytes
) -> None:
    from sqlalchemy import LargeBinary, bindparam, update

    from omnigent.db.db_models import SqlPreference

    store.save_order([], user_id=None)
    with store._engine.begin() as connection:
        connection.execute(
            update(SqlPreference)
            .where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "local",
                SqlPreference.key == "project_order",
            )
            .values(value=bindparam("raw", type_=LargeBinary)),
            {"raw": raw},
        )
    default = {"sort_mode": "alphabetical", "ordered_project_ids": None}
    assert store.get_order_preference(user_id=None) == default
    assert store.get_order(user_id=None) is None
    assert store.save_order(None, user_id=None) == default
    assert store.get_order_preference(user_id=None) == default
    store.save_order([], user_id=None)
    assert store.get_order(user_id=None) == []


def test_order_decoder_bounds_size_and_normalizes_duplicate_ids() -> None:
    import json

    from omnigent.db.compression import encode
    from omnigent.stores.project_store.sqlalchemy_store import _decode_order

    a, b = _uid("decode-a"), _uid("decode-b")
    for preference in ([b, a, b], {"sort_mode": "manual", "ordered_project_ids": [b, a, b]}):
        assert _decode_order(encode(json.dumps(preference))) == {
            "sort_mode": "manual",
            "ordered_project_ids": [b, a],
        }
    for preference in ([a] * 10001, ["x" * (512 * 1024)]):
        assert _decode_order(encode(json.dumps(preference))) == {
            "sort_mode": "alphabetical",
            "ordered_project_ids": None,
        }


def test_apply_order_deduplicates_and_appends_unranked_projects() -> None:
    from omnigent.stores.project_store import apply_project_order

    assert apply_project_order(
        ["b", "c", "a"], ["a", "deleted", "a"], project_id=str, project_name=str
    ) == ["a", "b", "c"]


# ── namespace tree ────────────────────────────────────────────────────────


def _tree(store: SqlAlchemyProjectStore, owner: str | None = "alice") -> dict[str, str]:
    """Create ``a`` > ``b`` > ``c`` plus root ``z``; return name -> id."""
    ids = {name: _uid(name) for name in ("a", "b", "c", "z")}
    store.create(ids["a"], "a", owner)
    store.create(ids["b"], "b", owner, parent_id=ids["a"])
    store.create(ids["c"], "c", owner, parent_id=ids["b"])
    store.create(ids["z"], "z", owner)
    return ids


def test_tree_walk_and_computed_path(store: SqlAlchemyProjectStore) -> None:
    """Paths are computed from ``parent_id`` and resolved case-insensitively."""
    ids = _tree(store)
    assert store.get(ids["c"], user_id="alice").parent_id == ids["b"]
    assert store.get_path(ids["c"], user_id="alice") == "a/b/c"
    assert store.get_path(ids["z"], user_id="alice") == "z"
    assert store.get_path(ids["c"], user_id="bob") is None
    assert [p.id for p in store.find_by_path("A/B/c", user_id="alice")] == [ids["c"]]
    assert store.find_by_path("a/c", user_id="alice") == []
    assert store.find_by_path("a//c", user_id="alice") == []
    assert store.find_by_path("a/b", user_id="bob") == []


def test_siblings_collide_only_under_the_same_parent(store: SqlAlchemyProjectStore) -> None:
    """The same name is fine under different parents, not under one."""
    ids = _tree(store)
    store.create(_uid("c2"), "c", "alice", parent_id=ids["a"])
    with pytest.raises(OmnigentError) as caught:
        store.create(_uid("c3"), "c", "alice", parent_id=ids["b"])
    assert caught.value.code == ErrorCode.ALREADY_EXISTS
    with pytest.raises(OmnigentError) as moved:
        store.update(_uid("c2"), user_id="alice", parent_id=ids["b"])
    assert moved.value.code == ErrorCode.ALREADY_EXISTS


def test_move_refuses_cycles(store: SqlAlchemyProjectStore) -> None:
    """A project cannot move under itself or a descendant."""
    ids = _tree(store)
    for target in (ids["a"], ids["c"]):
        with pytest.raises(OmnigentError) as caught:
            store.update(ids["a"], user_id="alice", parent_id=target)
        assert caught.value.code == ErrorCode.INVALID_INPUT
    assert store.get_path(ids["c"], user_id="alice") == "a/b/c"


def test_move_and_rename(store: SqlAlchemyProjectStore) -> None:
    """``parent_id=None`` moves to the root; omitting it leaves the parent alone."""
    ids = _tree(store)
    store.update(ids["b"], user_id="alice", name="b2")
    assert store.get_path(ids["c"], user_id="alice") == "a/b2/c"
    moved = store.update(ids["b"], user_id="alice", parent_id=ids["z"])
    assert moved is not None and moved.parent_id == ids["z"]
    assert store.get_path(ids["c"], user_id="alice") == "z/b2/c"
    store.update(ids["b"], user_id="alice", parent_id=None)
    assert store.get_path(ids["c"], user_id="alice") == "b2/c"


def test_depth_cap_applies_to_create_and_to_moved_subtrees(
    store: SqlAlchemyProjectStore,
) -> None:
    """Paths stop at 8 segments, counting the moved subtree's own height."""
    chain = [_uid(f"d{i}") for i in range(8)]
    parent = None
    for index, project_id in enumerate(chain):
        store.create(project_id, f"d{index}", "alice", parent_id=parent)
        parent = project_id
    with pytest.raises(OmnigentError) as too_deep:
        store.create(_uid("d8"), "d8", "alice", parent_id=chain[-1])
    assert too_deep.value.code == ErrorCode.INVALID_INPUT
    store.create(_uid("sub"), "sub", "alice")
    store.create(_uid("leaf"), "leaf", "alice", parent_id=_uid("sub"))
    with pytest.raises(OmnigentError):
        store.update(_uid("sub"), user_id="alice", parent_id=chain[-2])
    store.update(_uid("leaf"), user_id="alice", parent_id=chain[-2])


@pytest.mark.parametrize("bad", ["a/b", ".", ".."])
def test_new_and_renamed_names_reject_slash_and_dots(
    store: SqlAlchemyProjectStore, bad: str
) -> None:
    """Names are single path segments."""
    with pytest.raises(OmnigentError) as created:
        store.create(_uid("x"), bad, "alice")
    assert created.value.code == ErrorCode.INVALID_INPUT
    store.create(_uid("y"), "ok", "alice")
    with pytest.raises(OmnigentError):
        store.update(_uid("y"), user_id="alice", name=bad)


def test_missing_or_foreign_parent_is_rejected(store: SqlAlchemyProjectStore) -> None:
    """A parent must exist and belong to the same owner."""
    store.create(_uid("bobs"), "bobs", "bob")
    for parent in (_uid("nope"), _uid("bobs")):
        with pytest.raises(OmnigentError) as caught:
            store.create(_uid("x"), "x", "alice", parent_id=parent)
        assert caught.value.code == ErrorCode.INVALID_INPUT


def test_delete_with_children_conflicts_until_children_are_gone(
    store: SqlAlchemyProjectStore,
) -> None:
    """A parent cannot be deleted while it has children."""
    ids = _tree(store)
    with pytest.raises(OmnigentError) as caught:
        store.delete(ids["b"], user_id="alice")
    assert caught.value.code == ErrorCode.CONFLICT
    assert store.delete(ids["c"], user_id="alice") is True
    assert store.delete(ids["b"], user_id="alice") is True


def test_slash_migration_plan_reuses_existing_ancestors(
    store: SqlAlchemyProjectStore, db_uri: str
) -> None:
    """Legacy ``a/b`` names map onto the tree; clashes and bad segments are flagged."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlProject
    from omnigent.db.utils import get_or_create_engine
    from omnigent.stores.project_store import plan_slash_migration

    store.create(_uid("a"), "a", "alice")
    store.create(_uid("ab"), "b", "alice", parent_id=_uid("a"))
    # Legacy rows predate name validation, so write them through the ORM.
    with Session(get_or_create_engine(db_uri)) as session:
        for seed, name in (("l1", "a/x"), ("l2", "q/r/s"), ("l3", "a/b"), ("l4", "k//m")):
            session.add(SqlProject(id=_uid(seed), name=name, user_id="alice", created_at=1))
        session.commit()
    steps = {s.old_name: s for s in plan_slash_migration(store.list(user_id="alice"))}
    assert set(steps) == {"a/x", "q/r/s", "a/b", "k//m"}
    assert steps["a/x"].create == () and steps["a/x"].parent_path == "a"
    assert steps["a/x"].new_name == "x" and steps["a/x"].problem is None
    assert steps["q/r/s"].create == ("q", "q/r")
    assert "already project" in (steps["a/b"].problem or "")
    assert "empty" in (steps["k//m"].problem or "")


def _add_legacy_rows(db_uri: str, *rows: tuple[str, str, str | None]) -> None:
    """Insert ``(seed, name, parent_id)`` rows through the ORM, bypassing name validation."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlProject
    from omnigent.db.utils import get_or_create_engine

    with Session(get_or_create_engine(db_uri)) as session:
        for seed, name, parent_id in rows:
            session.add(
                SqlProject(
                    id=_uid(seed), name=name, user_id="alice", created_at=1, parent_id=parent_id
                )
            )
        session.commit()


def test_legacy_slash_project_cannot_be_a_parent_or_move_under_one(
    store: SqlAlchemyProjectStore, db_uri: str
) -> None:
    """Legacy ``x/y`` names stay roots until migrated: their subtree would be unaddressable."""
    _add_legacy_rows(db_uri, ("legacy", "x/y", None))
    store.create(_uid("plain"), "plain", "alice")
    with pytest.raises(OmnigentError) as as_parent:
        store.create(_uid("kid"), "kid", "alice", parent_id=_uid("legacy"))
    assert as_parent.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(OmnigentError) as moved_under:
        store.update(_uid("plain"), user_id="alice", parent_id=_uid("legacy"))
    assert moved_under.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(OmnigentError) as legacy_moved:
        store.update(_uid("legacy"), user_id="alice", parent_id=_uid("plain"))
    assert legacy_moved.value.code == ErrorCode.INVALID_INPUT
    assert store.get(_uid("legacy"), user_id="alice").parent_id is None


def test_legacy_name_survives_a_save_that_echoes_it_but_not_a_rename(
    store: SqlAlchemyProjectStore, db_uri: str
) -> None:
    """The segment rules apply only when the name changes."""
    _add_legacy_rows(db_uri, ("legacy", "x/y", None))
    echoed = store.update(_uid("legacy"), user_id="alice", name="x/y", config={"icon": "a"})
    assert echoed is not None and echoed.config == {"icon": "a"}
    with pytest.raises(OmnigentError) as renamed:
        store.update(_uid("legacy"), user_id="alice", name="p/q")
    assert renamed.value.code == ErrorCode.INVALID_INPUT


def test_find_by_path_falls_back_to_a_legacy_flat_name_and_flags_conflicts(
    store: SqlAlchemyProjectStore, db_uri: str
) -> None:
    """``x/y`` finds a legacy root named ``x/y``; a tree project at the same path conflicts."""
    _add_legacy_rows(db_uri, ("legacy", "x/y", None))
    assert [p.id for p in store.find_by_path("x/y", user_id="alice")] == [_uid("legacy")]
    store.create(_uid("x"), "x", "alice")
    store.create(_uid("y"), "y", "alice", parent_id=_uid("x"))
    with pytest.raises(OmnigentError) as caught:
        store.find_by_path("x/y", user_id="alice")
    assert caught.value.code == ErrorCode.CONFLICT


def test_orphan_is_walked_as_a_root(store: SqlAlchemyProjectStore, db_uri: str) -> None:
    """A project whose parent row is missing resolves by the same path ``get_path`` reports."""
    _add_legacy_rows(db_uri, ("orphan", "c", _uid("gone")))
    assert store.get_path(_uid("orphan"), user_id="alice") == "c"
    assert [p.id for p in store.find_by_path("c", user_id="alice")] == [_uid("orphan")]


def test_concurrent_moves_cannot_form_a_cycle(store: SqlAlchemyProjectStore) -> None:
    """Hold one move after its checks; the competing move waits and then sees the cycle.

    Runs on SQLite (writers serialize) and on OMNIGENT_TEST_DB_URI (row locks).
    """
    import threading

    first, second = _uid("race-a"), _uid("race-b")
    store.create(first, "a", "alice")
    store.create(second, "b", "alice")
    reached, release = threading.Event(), threading.Event()

    def hold_first_mover(operation: str) -> None:
        if threading.current_thread().name == "mover-1":
            reached.set()
            assert release.wait(timeout=15)

    store.after_checks_hook = hold_first_mover
    outcomes: dict[str, object] = {}

    def move(label: str, project_id: str, parent_id: str) -> None:
        try:
            store.update(project_id, user_id="alice", parent_id=parent_id)
            outcomes[label] = "moved"
        except OmnigentError as error:
            outcomes[label] = error.code

    movers = [
        threading.Thread(name="mover-1", target=move, args=("1", first, second)),
        threading.Thread(name="mover-2", target=move, args=("2", second, first)),
    ]
    movers[0].start()
    assert reached.wait(timeout=15)
    movers[1].start()
    movers[1].join(timeout=1)
    assert movers[1].is_alive(), "the competing move must wait for the first one's lock"
    release.set()
    for mover in movers:
        mover.join(timeout=15)
    assert outcomes == {"1": "moved", "2": ErrorCode.INVALID_INPUT}
    assert store.get_path(first, user_id="alice") == "b/a"


def test_create_under_a_parent_waits_for_a_concurrent_delete_of_it(
    store: SqlAlchemyProjectStore,
) -> None:
    """A create racing the parent's delete is serialized: it cannot leave an orphan."""
    import threading

    parent = _uid("race-parent")
    store.create(parent, "p", "alice")
    reached, release = threading.Event(), threading.Event()

    def hold_deleter(operation: str) -> None:
        if threading.current_thread().name == "deleter":
            reached.set()
            assert release.wait(timeout=15)

    store.after_checks_hook = hold_deleter
    outcomes: dict[str, object] = {}

    def delete() -> None:
        outcomes["delete"] = store.delete(parent, user_id="alice")

    def create() -> None:
        try:
            store.create(_uid("race-kid"), "kid", "alice", parent_id=parent)
            outcomes["create"] = "created"
        except OmnigentError as error:
            outcomes["create"] = error.code

    threads = [threading.Thread(name="deleter", target=delete), threading.Thread(target=create)]
    threads[0].start()
    assert reached.wait(timeout=15)
    threads[1].start()
    threads[1].join(timeout=1)
    assert threads[1].is_alive()
    release.set()
    for thread in threads:
        thread.join(timeout=15)
    assert outcomes == {"delete": True, "create": ErrorCode.INVALID_INPUT}
    assert store.find_by_path("p/kid", user_id="alice") == []


def test_delete_sees_a_child_created_while_it_waited_for_the_parent_lock(
    store: SqlAlchemyProjectStore,
) -> None:
    """Create holds the lock first: the delete then waits, sees the child and refuses.

    The Postgres run is the one that bites: there the waiting delete's locking read
    keeps its older snapshot.
    """
    import threading

    parent, kid = _uid("race-first-parent"), _uid("race-first-kid")
    store.create(parent, "p", "alice")
    reached, release = threading.Event(), threading.Event()

    def hold_creator(operation: str) -> None:
        if threading.current_thread().name == "creator":
            reached.set()
            assert release.wait(timeout=15)

    store.after_checks_hook = hold_creator
    outcomes: dict[str, object] = {}

    def create() -> None:
        store.create(kid, "kid", "alice", parent_id=parent)
        outcomes["create"] = "created"

    def delete() -> None:
        try:
            outcomes["delete"] = store.delete(parent, user_id="alice")
        except OmnigentError as error:
            outcomes["delete"] = error.code

    threads = [threading.Thread(name="creator", target=create), threading.Thread(target=delete)]
    threads[0].start()
    assert reached.wait(timeout=15)
    threads[1].start()
    threads[1].join(timeout=1)
    assert threads[1].is_alive(), "the delete must wait for the create's lock"
    release.set()
    for thread in threads:
        thread.join(timeout=15)
    assert outcomes == {"create": "created", "delete": ErrorCode.CONFLICT}
    assert store.get_path(kid, user_id="alice") == "p/kid"


def test_migrate_slashes_step_applies_as_one_update(
    store: SqlAlchemyProjectStore, db_uri: str
) -> None:
    """The planned rename-and-move lands in one update; a move keeping the name is refused."""
    from omnigent.stores.project_store import plan_slash_migration

    _add_legacy_rows(db_uri, ("legacy", "a/b", None))
    parent = store.create(_uid("a"), "a", "alice")
    [step] = plan_slash_migration(store.list(user_id="alice"))
    assert (step.new_name, step.parent_path) == ("b", "a")

    moved = store.update(step.project_id, user_id="alice", name=step.new_name, parent_id=parent.id)

    assert moved is not None and moved.parent_id == parent.id
    assert store.get_path(step.project_id, user_id="alice") == "a/b"


def test_a_dashed_id_reaches_the_same_project_everywhere(store: SqlAlchemyProjectStore) -> None:
    """Dashed and hex forms of one UUID agree across lookups, writes and parent links."""
    project, kid = _uid("dashed"), _uid("dashed-kid")
    dashed = str(uuid.UUID(project))
    store.create(project, "n", "alice")
    assert store.get(dashed, user_id="alice").id == project
    assert store.get_path(dashed, user_id="alice") == "n"
    assert store.update(dashed, user_id="alice", name="m").name == "m"
    assert store.create(kid, "kid", "alice", parent_id=dashed).parent_id == project
    store.update(kid, user_id="alice", parent_id=None)
    assert store.update(kid, user_id="alice", parent_id=dashed).parent_id == project
    store.update(kid, user_id="alice", parent_id=None)
    assert store.delete(dashed, user_id="alice") is True
    assert store.get(project, user_id="alice") is None


# Atomic delete with sessions. Runs on SQLite and on OMNIGENT_TEST_DB_URI.


@pytest.fixture()
def conversations(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _allow(_session_ids: list[str]) -> None:
    return None


def _project_with_members(
    store: SqlAlchemyProjectStore,
    conversations: SqlAlchemyConversationStore,
    *,
    count: int = 3,
) -> tuple[str, str, list[str]]:
    """A project whose members carry both the id and the legacy label."""
    project_id, name = _uid("sweep"), f"sweep-{uuid.uuid4().hex[:8]}"
    store.create(project_id, name, "alice")
    member_ids = []
    for _ in range(count):
        member = conversations.create_conversation(title="member")
        assert conversations.set_conversation_project(member.id, project_id)
        conversations.set_labels(member.id, {PROJECT_LABEL_KEY: name})
        member_ids.append(member.id)
    return project_id, name, member_ids


def _assert_untouched(
    store: SqlAlchemyProjectStore,
    conversations: SqlAlchemyConversationStore,
    project_id: str,
    name: str,
    member_ids: list[str],
) -> None:
    assert store.get(project_id, user_id="alice") is not None
    for member_id in member_ids:
        member = conversations.get_conversation(member_id)
        assert member.archived is False
        assert member.project_id == project_id
        assert member.labels[PROJECT_LABEL_KEY] == name
        assert ARCHIVED_AT_LABEL_KEY not in member.labels


def _delete(
    store: SqlAlchemyProjectStore,
    conversations: SqlAlchemyConversationStore,
    project_id: str,
    authorize=_allow,
):
    return store.delete_with_sessions(
        project_id, user_id="alice", conversation_store=conversations, authorize=authorize
    )


def test_delete_with_sessions_archives_unfiles_and_removes_the_row(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    project_id, _name, member_ids = _project_with_members(store, conversations)

    sweep = _delete(store, conversations, project_id)

    assert sweep is not None
    assert sorted(sweep.session_ids) == sorted(member_ids)
    assert sorted(sweep.newly_archived_ids) == sorted(member_ids)
    assert store.get(project_id, user_id="alice") is None
    for member_id in member_ids:
        member = conversations.get_conversation(member_id)
        assert member.archived is True
        assert member.project_id is None
        assert PROJECT_LABEL_KEY not in member.labels
        assert member.labels[ARCHIVED_AT_LABEL_KEY] == str(sweep.updated_at)
        assert member.updated_at == sweep.updated_at


def test_delete_with_sessions_keeps_an_archived_members_retention_clock(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    project_id, _name, member_ids = _project_with_members(store, conversations, count=2)
    conversations.update_conversation_with_changes(member_ids[0], archived=True)
    archived_at = conversations.get_conversation(member_ids[0]).labels[ARCHIVED_AT_LABEL_KEY]
    conversations.set_labels(member_ids[0], {ARCHIVED_AT_LABEL_KEY: "1"})

    sweep = _delete(store, conversations, project_id)

    assert archived_at
    assert sweep is not None
    assert sweep.newly_archived_ids == (member_ids[1],)
    kept = conversations.get_conversation(member_ids[0])
    assert kept.labels[ARCHIVED_AT_LABEL_KEY] == "1"
    assert kept.project_id is None


def test_delete_with_sessions_of_an_unowned_project_changes_nothing(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    project_id, name, member_ids = _project_with_members(store, conversations)

    swept = store.delete_with_sessions(
        project_id, user_id="mallory", conversation_store=conversations, authorize=_allow
    )

    assert swept is None
    _assert_untouched(store, conversations, project_id, name, member_ids)


@pytest.mark.parametrize("failing_step", ["delete_members", "delete_row"])
def test_delete_with_sessions_failure_leaves_every_member_as_it_was(
    store: SqlAlchemyProjectStore,
    conversations: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """A failure after some members were written, or at the row delete, undoes all of it."""
    monkeypatch.setattr(conversation_module, "_PROJECT_SWEEP_CHUNK_SIZE", 2)
    project_id, name, member_ids = _project_with_members(store, conversations)
    written: list[bool] = []

    def fail(operation: str) -> None:
        if operation == failing_step:
            with conversations._session("count_written") as session:
                written.append(bool(session.execute(_archived_count(member_ids)).scalar()))
            raise RuntimeError("injected")

    store.after_checks_hook = fail
    with pytest.raises(RuntimeError, match="injected"):
        _delete(store, conversations, project_id)
    store.after_checks_hook = None

    assert written == [False]
    _assert_untouched(store, conversations, project_id, name, member_ids)


def _archived_count(member_ids: list[str]):
    from sqlalchemy import func, select

    from omnigent.db.db_models import SqlConversation

    return select(func.count()).where(
        SqlConversation.id.in_(member_ids), SqlConversation.archived.is_(True)
    )


def test_delete_with_sessions_refused_by_authorize_changes_nothing(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    project_id, name, member_ids = _project_with_members(store, conversations)
    seen: list[str] = []

    def refuse(session_ids: list[str]) -> None:
        seen.extend(session_ids)
        raise OmnigentError("no", code=ErrorCode.FORBIDDEN)

    with pytest.raises(OmnigentError) as refusal:
        _delete(store, conversations, project_id, refuse)

    assert refusal.value.code == ErrorCode.FORBIDDEN
    assert sorted(seen) == sorted(member_ids)
    _assert_untouched(store, conversations, project_id, name, member_ids)


def test_delete_with_sessions_of_a_parent_changes_nothing(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    project_id, name, member_ids = _project_with_members(store, conversations)
    store.create(_uid("sweep-kid"), "kid", "alice", parent_id=project_id)

    with pytest.raises(OmnigentError) as refusal:
        _delete(store, conversations, project_id)

    assert refusal.value.code == ErrorCode.CONFLICT
    _assert_untouched(store, conversations, project_id, name, member_ids)


def test_delete_with_sessions_sweeps_a_session_filed_while_it_waited(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    """The filing commits first: the delete waits on its lock, then reads and sweeps it."""
    import threading

    from sqlalchemy import update

    from omnigent.db.db_models import SqlConversationMetadata

    project_id, _name, member_ids = _project_with_members(store, conversations, count=1)
    late = conversations.create_conversation(title="late")
    outcome: dict[str, object] = {}

    def delete() -> None:
        outcome["sweep"] = _delete(store, conversations, project_id)

    deleter = threading.Thread(target=delete)
    with conversations._session_immediate("file_during_delete") as session:
        assert conversations._live_project_id(session, project_id) == project_id
        session.execute(
            update(SqlConversationMetadata)
            .where(SqlConversationMetadata.id == late.id)
            .values(project_id=project_id)
        )
        deleter.start()
        deleter.join(timeout=1)
        assert deleter.is_alive()
    deleter.join(timeout=15)

    sweep = outcome["sweep"]
    assert sorted(sweep.session_ids) == sorted([*member_ids, late.id])
    stored = conversations.get_conversation(late.id)
    assert (stored.archived, stored.project_id) == (True, None)
    assert store.get(project_id, user_id="alice") is None


def test_delete_with_sessions_leaves_a_member_unfiled_while_it_waited(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore
) -> None:
    """Unfiling locks no project row, so only the member row lock stops the sweep."""
    import threading

    from sqlalchemy import update

    from omnigent.db.db_models import SqlConversationMetadata

    project_id, _name, member_ids = _project_with_members(store, conversations, count=2)
    unfiled, staying = member_ids
    conversations.delete_label(unfiled, PROJECT_LABEL_KEY)
    outcome: dict[str, object] = {}

    def delete() -> None:
        outcome["sweep"] = _delete(store, conversations, project_id)

    deleter = threading.Thread(target=delete)
    with conversations._session_immediate("unfile_during_delete") as session:
        session.execute(
            update(SqlConversationMetadata)
            .where(SqlConversationMetadata.id == unfiled)
            .values(project_id=None)
        )
        deleter.start()
        deleter.join(timeout=1)
        assert deleter.is_alive()
    deleter.join(timeout=15)

    assert outcome["sweep"].session_ids == (staying,)
    stored = conversations.get_conversation(unfiled)
    assert (stored.archived, stored.project_id) == (False, None)


def _file_late(conversations, late_id, project_id):
    return conversations.set_conversation_project(late_id, project_id)


def _create_late(conversations, late_id, project_id):
    del late_id
    return conversations.create_conversation(title="new", project_id=project_id).id


def _fork_late(conversations, late_id, project_id):
    return conversations.fork_conversation(late_id, project_id=project_id).id


@pytest.mark.parametrize("file_it", [_file_late, _create_late, _fork_late])
def test_filing_during_a_delete_waits_and_never_dangles(
    store: SqlAlchemyProjectStore, conversations: SqlAlchemyConversationStore, file_it
) -> None:
    """The delete holds the lock first: the filing waits, then finds no project."""
    import threading

    project_id, _name, _members = _project_with_members(store, conversations, count=1)
    late = conversations.create_conversation(title="late")
    reached, release = threading.Event(), threading.Event()

    def hold_deleter(operation: str) -> None:
        if operation == "delete_row":
            reached.set()
            assert release.wait(timeout=15)

    store.after_checks_hook = hold_deleter
    outcome: dict[str, object] = {}

    def delete() -> None:
        outcome["sweep"] = _delete(store, conversations, project_id)

    def file_session() -> None:
        outcome["filed"] = file_it(conversations, late.id, project_id)

    deleter, filer = threading.Thread(target=delete), threading.Thread(target=file_session)
    deleter.start()
    assert reached.wait(timeout=15)
    filer.start()
    filer.join(timeout=1)
    assert filer.is_alive()
    release.set()
    deleter.join(timeout=15)
    filer.join(timeout=15)
    store.after_checks_hook = None

    assert outcome["sweep"] is not None
    assert store.get(project_id, user_id="alice") is None
    filed = outcome["filed"]
    if isinstance(filed, bool):
        assert filed is False
        assert conversations.get_conversation(late.id).project_id is None
    else:
        assert conversations.get_conversation(filed).project_id is None

"""SQLAlchemy-backed project store."""

from __future__ import annotations

import builtins
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import zstandard
from sqlalchemy import LargeBinary, asc, select, type_coerce, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode, encode
from omnigent.db.db_models import SqlPreference, SqlProject, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.group_thread_store import SqlAlchemyGroupThreadStore
from omnigent.stores.project_store import (
    MAX_PROJECT_DEPTH,
    UNSET,
    ProjectOrderPreference,
    ProjectStore,
    _Unset,
    effective_private_ids,
    lookup_project_path,
    project_path,
    validate_project_segment,
)
from omnigent.stores.role_store.sqlalchemy_store import SqlAlchemyRoleStore
from omnigent.stores.saved_view_store import SqlAlchemySavedViewStore
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore, ProjectSessionSweep

# Max serialized length of a project's config blob. The value is persisted
# verbatim and reflected back on every read, so an unbounded blob is a mild
# storage/response-size amplifier on an otherwise cheap CRUD row. 64 KiB is far
# above any realistic set of default-session hints (a few short keys) while
# still capping abuse.
_CONFIG_MAX_SERIALIZED_LEN = 64 * 1024
_PROJECT_ORDER_KEY = "project_order"
_PREFERENCE_MAX_STORED_BYTES = 65535


def _encode_config(config: dict[str, Any] | None) -> str | None:
    """Pack a project's config dict into a compact JSON blob for storage.

    An empty or ``None`` config stores SQL ``NULL`` rather than ``"{}"``, so
    "no defaults" is one canonical representation.

    :param config: The config object, or ``None``.
    :returns: Compact JSON object string, or ``None`` when empty.
    :raises OmnigentError: ``INVALID_INPUT`` if the serialized config exceeds
        :data:`_CONFIG_MAX_SERIALIZED_LEN`.
    """
    if not config:
        return None
    blob = json.dumps(config, separators=(",", ":"))
    if len(blob) > _CONFIG_MAX_SERIALIZED_LEN:
        raise OmnigentError(
            f"project config too large ({len(blob)} bytes; max {_CONFIG_MAX_SERIALIZED_LEN})",
            code=ErrorCode.INVALID_INPUT,
        )
    return blob


def _decode_config(raw: str | None) -> dict[str, Any]:
    """Unpack the stored ``config`` blob to a dict (``{}`` when unset).

    Defensive against a non-object blob: the encode path only ever writes JSON
    objects, but a future writer or a manual DB edit could store a scalar/array,
    which would otherwise flow back as a non-dict. Coerce anything that isn't a
    dict to ``{}`` so callers can always treat config as a mapping.

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded object, or an empty dict when ``NULL`` / empty / non-object.
    """
    if not raw:
        return {}
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else {}


def _canonical_id(project_id: str) -> str:
    """The stored hex form of an id; a non-UUID passes through and matches nothing."""
    try:
        return uuid_to_bytes(project_id).hex()
    except ValueError:
        return project_id


def _to_entity(row: SqlProject) -> Project:
    """
    Convert a :class:`SqlProject` ORM row to a :class:`Project`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Project` dataclass instance.
    """
    return Project(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        config=_decode_config(row.config),
        parent_id=row.parent_id,
        private=bool(row.private),
    )


def _encode_order(preference: ProjectOrderPreference) -> str:
    """Serialize an order only if its compressed frame fits a MySQL BLOB."""
    serialized = json.dumps(preference, separators=(",", ":"))
    stored = encode(serialized)
    assert stored is not None
    if len(stored) > _PREFERENCE_MAX_STORED_BYTES:
        raise OmnigentError("Project order is too large to save", code=ErrorCode.INVALID_INPUT)
    return serialized


def _decode_order(raw: bytes | str | memoryview | None) -> ProjectOrderPreference:
    """Keep invalid preferences from breaking project discovery."""
    default: ProjectOrderPreference = {"sort_mode": "alphabetical", "ordered_project_ids": None}
    if raw is None:
        return default
    try:
        # The 10,000-ID API limit serializes to under 400 KiB, including legacy spacing.
        decoded = json.loads(decode(raw, max_decoded_bytes=512 * 1024) or "null")
        # The original format stored only the manual ID array.
        if isinstance(decoded, list):
            decoded = {"sort_mode": "manual", "ordered_project_ids": decoded}
        if not isinstance(decoded, dict) or decoded.get("sort_mode") not in (
            "alphabetical",
            "manual",
        ):
            return default
        ids = decoded.get("ordered_project_ids")
        if ids is None:
            return default
        if (
            not isinstance(ids, list)
            or len(ids) > 10000
            or any(
                not isinstance(id, str) or re.fullmatch("[0-9a-f]{32}", id) is None for id in ids
            )
        ):
            return default
        return {
            "sort_mode": decoded["sort_mode"],
            "ordered_project_ids": list(dict.fromkeys(ids)),
        }
    except (ValueError, RecursionError, zstandard.ZstdError):
        return default


class SqlAlchemyProjectStore(ProjectStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectStore`.

    Persists projects in a relational database via the SQLAlchemy ORM. Every
    query is scoped by ``workspace_id`` (tenant partition) and ``user_id``
    (projects are owner-private).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project store.

        Creates or reuses a SQLAlchemy engine and session factory for the given
        database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
            immediate=True,
        )
        self.roles = SqlAlchemyRoleStore(storage_location)
        self.group_threads = SqlAlchemyGroupThreadStore(storage_location)
        self.saved_views = SqlAlchemySavedViewStore(storage_location)
        # Test seam: called inside a write after its checks pass, before commit,
        # so a test can hold one writer there and start a competing one.
        self.after_checks_hook: Callable[[str], None] | None = None

    def _after_checks(self, operation: str) -> None:
        if self.after_checks_hook is not None:
            self.after_checks_hook(operation)

    def _name_taken(
        self,
        session: Session,
        *,
        user_id: str | None,
        parent_id: str | None,
        name: str,
        exclude_id: str | None,
    ) -> bool:
        """Return whether ``user_id`` already has a sibling named ``name`` under ``parent_id``.

        The sole enforcement point for sibling-name uniqueness: there is no
        unique index behind it (see ``SqlProject.__table_args__``). Being a
        check-then-write, it cannot close the concurrency window: two
        simultaneous creates or renames to the same name can both land.

        :param session: The active SQLAlchemy session.
        :param user_id: The owner scope.
        :param parent_id: The parent scope (``None`` = roots).
        :param name: The candidate name.
        :param exclude_id: A project id to exclude (the row being renamed).
        :returns: ``True`` if another sibling has this name.
        """
        stmt = select(SqlProject.id).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.user_id == user_id,
            SqlProject.parent_id.is_(None)
            if parent_id is None
            else SqlProject.parent_id == parent_id,
            SqlProject.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProject.id != exclude_id)
        return session.execute(stmt).first() is not None

    def _owner_rows(
        self, session: Session, user_id: str | None, *, lock: bool = False
    ) -> dict[str, SqlProject]:
        """All of the owner's project rows by id (owners hold a handful).

        :param lock: Take row locks (``SELECT ... FOR UPDATE``) so tree writes by
            one owner serialize on Postgres/MySQL. SQLite already serializes writers
            (``BEGIN IMMEDIATE``) and ignores the clause.
        """
        stmt = (
            select(SqlProject)
            .where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.user_id == user_id,
            )
            .order_by(SqlProject.id)
        )
        if lock:
            stmt = stmt.with_for_update()
        return {row.id: row for row in session.execute(stmt).scalars()}

    @staticmethod
    def _depth(rows: dict[str, SqlProject], project_id: str | None) -> int:
        """Segments in the path ending at ``project_id`` (0 for ``None``), cycle-guarded."""
        depth = 0
        seen: set[str] = set()
        while project_id is not None and project_id in rows and project_id not in seen:
            seen.add(project_id)
            depth += 1
            project_id = rows[project_id].parent_id
        return depth

    @staticmethod
    def _height(rows: dict[str, SqlProject], project_id: str) -> int:
        """Segments in the longest path down from ``project_id`` (itself counts as 1)."""
        children: dict[str, list[str]] = {}
        for row in rows.values():
            if row.parent_id is not None:
                children.setdefault(row.parent_id, []).append(row.id)
        height, frontier, seen = 0, [project_id], {project_id}
        while frontier:
            height += 1
            frontier = [c for p in frontier for c in children.get(p, []) if c not in seen]
            seen.update(frontier)
        return height

    def _check_parent(
        self,
        rows: dict[str, SqlProject],
        *,
        project_id: str,
        parent_id: str | None,
        subtree_height: int,
    ) -> None:
        """Validate ``parent_id`` for ``project_id``: it exists, no cycle, depth cap holds."""
        if parent_id is None:
            return
        if parent_id not in rows:
            raise OmnigentError("Parent project not found", code=ErrorCode.INVALID_INPUT)
        if "/" in rows[parent_id].name:
            raise OmnigentError(
                f"Project {rows[parent_id].name!r} has a legacy name containing '/' and cannot "
                "have children; run `omnigent projects migrate-slashes --dry-run`",
                code=ErrorCode.INVALID_INPUT,
            )
        ancestor: str | None = parent_id
        seen: set[str] = set()
        while ancestor is not None and ancestor not in seen:
            if ancestor == project_id:
                raise OmnigentError(
                    "A project cannot be moved under itself or its descendants",
                    code=ErrorCode.INVALID_INPUT,
                )
            seen.add(ancestor)
            ancestor = rows[ancestor].parent_id if ancestor in rows else None
        if self._depth(rows, parent_id) + subtree_height > MAX_PROJECT_DEPTH:
            raise OmnigentError(
                f"Projects can be nested at most {MAX_PROJECT_DEPTH} levels deep",
                code=ErrorCode.INVALID_INPUT,
            )

    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Project:
        """Insert a new, empty project.

        Rejects a name a sibling already uses via the ``_name_taken`` pre-check.
        That check is the only guard (no unique index backs it), so a
        concurrent create of the same name can slip through; see
        ``SqlProject.__table_args__`` for why that is acceptable.
        """
        created_at = now_epoch()
        encoded_config = _encode_config(config)
        validate_project_segment(name)
        canonical_parent = None if parent_id is None else _canonical_id(parent_id)

        def write(session: Session) -> Project:
            require_active_account(session, user_id)
            rows = self._owner_rows(session, user_id, lock=True)
            if canonical_parent is not None:
                self._check_parent(
                    rows, project_id=project_id, parent_id=canonical_parent, subtree_height=1
                )
            if self._name_taken(
                session, user_id=user_id, parent_id=canonical_parent, name=name, exclude_id=None
            ):
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            row = SqlProject(
                id=project_id,
                name=name,
                user_id=user_id,
                created_at=created_at,
                updated_at=None,
                config=encoded_config,
                parent_id=canonical_parent,
            )
            session.add(row)
            session.flush()
            self._after_checks("create")
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_project", write)

    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """Return an owned project by id, or ``None`` if not found."""
        with self._session("select_project_by_id") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            return _to_entity(row)

    def list(self, *, user_id: str | None) -> list[Project]:
        """List the owner's projects ordered by ``created_at ASC, id ASC``."""
        with self._session("list_projects") as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.user_id == user_id)
                .order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        parent_id: str | _Unset | None = UNSET,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``. A ``config`` of ``{}`` clears
        the stored defaults (distinct from ``None`` = leave unchanged).

        A rename or move re-checks ``_name_taken``, which (as on ``create``) is
        the only uniqueness guard, so concurrent renames to the same name can
        both land. A move (``parent_id``) also refuses cycles and paths deeper
        than ``MAX_PROJECT_DEPTH``.
        """
        encoded_config = _encode_config(config) if config is not None else None
        updated_at = now_epoch()
        project_key = _canonical_id(project_id)

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            rows = self._owner_rows(session, user_id, lock=True)
            row = rows.get(project_key)
            if row is None:
                return None
            changed = False
            new_name = row.name if name is None else name
            new_parent = (
                row.parent_id
                if isinstance(parent_id, _Unset)
                else None
                if parent_id is None
                else _canonical_id(parent_id)
            )
            if new_name != row.name:
                # A legacy name is only re-validated when it actually changes.
                validate_project_segment(new_name)
            if new_name != row.name or new_parent != row.parent_id:
                if new_parent != row.parent_id:
                    if new_parent is not None and "/" in new_name:
                        raise OmnigentError(
                            f"Project {new_name!r} has a legacy name containing '/' and cannot "
                            "be moved under a parent; run `omnigent projects migrate-slashes "
                            "--dry-run`",
                            code=ErrorCode.INVALID_INPUT,
                        )
                    self._check_parent(
                        rows,
                        project_id=project_key,
                        parent_id=new_parent,
                        subtree_height=self._height(rows, project_key),
                    )
                if self._name_taken(
                    session,
                    user_id=user_id,
                    parent_id=new_parent,
                    name=new_name,
                    exclude_id=project_key,
                ):
                    raise OmnigentError(
                        f"A project named {new_name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = new_name
                row.parent_id = new_parent
                changed = True
            self._after_checks("update")
            if config is not None:
                if row.config != encoded_config:
                    row.config = encoded_config
                    changed = True
            if changed:
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_project", write)

    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found."""
        project_key = _canonical_id(project_id)

        def write(session: Session) -> bool:
            return self._delete_row(session, project_key, user_id)

        return run_write_transaction(self._session_immediate, "delete_project", write)

    def _delete_row(
        self,
        session: Session,
        project_key: str,
        user_id: str | None,
        before_delete: Callable[[SqlProject], None] | None = None,
    ) -> bool:
        """Delete an owned, childless project inside the caller's transaction.

        :param session: The open write transaction.
        :param project_key: The canonical project id.
        :param user_id: The requesting owner.
        :param before_delete: Runs after the locks and checks, before the row goes.
        :returns: ``False`` if not found / not owned.
        :raises OmnigentError: ``CONFLICT`` while the project has children.
        """
        rows = self._owner_rows(session, user_id, lock=True)
        row = rows.get(project_key)
        if row is None:
            return False
        # A fresh statement: the locking read above keeps the snapshot it took before
        # waiting, so it cannot see a child committed by the lock holder.
        child = (
            select(SqlProject.id)
            .where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.user_id == user_id,
                SqlProject.parent_id == project_key,
            )
            .limit(1)
            .with_for_update()
        )
        if session.execute(child).first() is not None:
            raise OmnigentError(
                "Project has child projects; move or delete them first",
                code=ErrorCode.CONFLICT,
            )
        self._after_checks("delete")
        if before_delete is not None:
            before_delete(row)
            self._after_checks("delete_row")
        session.delete(row)
        return True

    def member_session_ids(
        self,
        project_id: str,
        *,
        user_id: str | None,
        conversation_store: ConversationStore,
    ) -> builtins.list[str]:
        """List a project's members unlocked; empty if not found / not owned."""
        with self._session("select_project_members") as session:
            row = session.get(SqlProject, (current_workspace_id(), _canonical_id(project_id)))
            if row is None or row.user_id != user_id:
                return []
            return conversation_store.project_member_ids(
                session,
                project_id=row.id,
                legacy_name=None if row.parent_id is not None else row.name,
                owner_id=user_id,
            )

    def delete_with_sessions(
        self,
        project_id: str,
        *,
        user_id: str | None,
        conversation_store: ConversationStore,
        authorize: Callable[[builtins.list[str]], None],
    ) -> ProjectSessionSweep | None:
        """Archive and unfile the members and delete the project, all or nothing."""
        project_key = _canonical_id(project_id)

        def write(session: Session) -> ProjectSessionSweep | None:
            swept: builtins.list[ProjectSessionSweep] = []

            def archive_members(row: SqlProject) -> None:
                swept.append(
                    conversation_store.archive_project_members(
                        session,
                        project_id=row.id,
                        legacy_name=None if row.parent_id is not None else row.name,
                        owner_id=user_id,
                        authorize=authorize,
                        after_chunk=lambda: self._after_checks("delete_members"),
                    )
                )

            if not self._delete_row(session, project_key, user_id, archive_members):
                return None
            return swept[0]

        return run_write_transaction(
            self._session_immediate, "delete_project_with_sessions", write
        )

    def set_private(
        self, project_id: str, *, user_id: str | None, private: bool
    ) -> Project | None:
        """Set the owned project's private flag; ``None`` if not found."""
        updated_at = now_epoch()
        project_key = _canonical_id(project_id)

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = self._owner_rows(session, user_id, lock=True).get(project_key)
            if row is None:
                return None
            if bool(row.private) != private:
                row.private = private
                row.updated_at = updated_at
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "set_project_private", write)

    def project_privacy(self, *, user_id: str | None) -> dict[str, bool]:
        """Map every owned project id to its effective (inherited) private flag."""
        with self._session("select_project_privacy") as session:
            rows = self._owner_rows(session, user_id)
            parents = {pid: row.parent_id for pid, row in rows.items()}
            flagged = {pid for pid, row in rows.items() if row.private}
        private_ids = effective_private_ids(parents, flagged)
        return {pid: pid in private_ids for pid in parents}

    def get_path(self, project_id: str, *, user_id: str | None) -> str | None:
        """Return the owned project's namespace path (names joined by ``/``)."""
        with self._session("select_project_path") as session:
            rows = self._owner_rows(session, user_id)
            project_key = _canonical_id(project_id)
            if project_key not in rows:
                return None
            by_id = {pid: _to_entity(row) for pid, row in rows.items()}
        return project_path(by_id[project_key], by_id)

    def find_by_path(self, path: str, *, user_id: str | None) -> builtins.list[Project]:
        """Tree-walk ``path`` over the owner's projects (case-insensitive segments)."""
        if any(not segment for segment in path.split("/")):
            return []
        with self._session("select_projects_by_path") as session:
            projects = [_to_entity(row) for row in self._owner_rows(session, user_id).values()]
        tree, flat = lookup_project_path(projects, path)
        if tree and flat and flat[0].id != tree[0].id:
            raise OmnigentError(
                f"Path {path!r} matches both tree project {tree[0].id} and legacy project "
                f"{flat[0].id}. Run `omnigent projects migrate-slashes --dry-run`.",
                code=ErrorCode.CONFLICT,
            )
        return tree or flat

    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Return the active order used by project-list endpoints."""
        preference = self.get_order_preference(user_id=user_id)
        return preference["ordered_project_ids"] if preference["sort_mode"] == "manual" else None

    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read project ordering from the user's preferences."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session("read_project_order") as session:
            # Decode raw bytes here so malformed values cannot fail in the ORM result processor.
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == _PROJECT_ORDER_KEY,
                )
            )
            return _decode_order(raw)

    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Validate project ownership and update only the user's preference."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> ProjectOrderPreference:
            require_active_account(session, user_id)
            workspace_id = current_workspace_id()
            if ids is None:
                raw = session.scalar(
                    select(type_coerce(SqlPreference.value, LargeBinary))
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .with_for_update()
                )
                preference = _decode_order(raw)
                preference["sort_mode"] = "alphabetical"
                if raw is None:
                    return preference
                session.execute(
                    update(SqlPreference)
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .values(value=_encode_order(preference))
                )
                return preference
            owned = set(
                session.scalars(
                    select(SqlProject.id).where(
                        SqlProject.workspace_id == workspace_id,
                        SqlProject.user_id == user_id,
                    )
                )
            )
            if len(set(ids)) != len(ids) or not set(ids).issubset(owned):
                raise OmnigentError("Invalid project order", code=ErrorCode.INVALID_INPUT)
            preference: ProjectOrderPreference = {
                "sort_mode": "manual",
                "ordered_project_ids": ids,
            }
            encoded = _encode_order(preference)
            values = {
                "workspace_id": workspace_id,
                "user_id": preference_user_id,
                "key": _PROJECT_ORDER_KEY,
                "value": encoded,
            }
            dialect = self._engine.dialect.name
            stmt: Insert
            if dialect == "mysql":
                stmt = (
                    mysql_insert(SqlPreference)
                    .values(**values)
                    .on_duplicate_key_update(value=encoded)
                )
            elif dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            else:
                stmt = (
                    pg_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            session.execute(stmt)
            return preference

        return run_write_transaction(self._session_immediate, "save_project_order", write)

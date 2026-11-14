"""SQLAlchemy-backed role store."""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from typing import TypeVar, cast

from sqlalchemy import CursorResult, Select, asc, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlProject,
    SqlRole,
    SqlRoleEvent,
    SqlRoleLane,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_role_event_actor,
    decode_role_event_kind,
    encode_role_event_actor,
    encode_role_event_kind,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Role, RoleEvent, RoleLane
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.role_store import RoleStore
from omnigent.util.uuid import normalize_uuid

_IN_CHUNK = 500

_Item = TypeVar("_Item")


def _chunks(items: Iterable[_Item], size: int) -> Iterator[list[_Item]]:
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _project_key(project_id: str | None) -> str:
    """The name index's namespace key: bare hex, or ``""`` for the unfiled root."""
    return normalize_uuid(project_id) or ""


def _to_entity(row: SqlRole) -> Role:
    return Role(
        id=row.id,
        user_id=row.user_id,
        project_id=row.project_id,
        name=row.name,
        name_key=row.name_key,
        overlay_ref=row.overlay_ref,
        defaults=row.defaults,
        permanent=row.permanent,
        holder_session_id=row.holder_session_id,
        previous_session_id=row.previous_session_id,
        held_since=row.held_since,
        created_at=row.created_at,
        updated_at=row.updated_at,
        generation=row.generation,
    )


def _event_entity(row: SqlRoleEvent) -> RoleEvent:
    return RoleEvent(
        id=row.id,
        role_id=row.role_id,
        generation=row.generation,
        kind=decode_role_event_kind(row.kind),
        session_id=row.session_id,
        prev_session_id=row.prev_session_id,
        actor=decode_role_event_actor(row.actor),
        reason=row.reason,
        at=row.at,
        role_name=row.role_name,
        role_path=row.role_path,
    )


def _lane_entity(row: SqlRoleLane) -> RoleLane:
    return RoleLane(
        id=row.id,
        role_id=row.role_id,
        name=row.name,
        session_id=row.session_id,
        purpose=row.purpose,
        item=row.item,
        created_at=row.created_at,
    )


def _stored_overlay(name: str, overlay_ref: str | None) -> str | None:
    """The stored form of an overlay: ``None`` when it is just the role's name."""
    return (
        None if overlay_ref is None or overlay_ref.casefold() == name.casefold() else overlay_ref
    )


class SqlAlchemyRoleStore(RoleStore):
    """SQLAlchemy implementation of :class:`RoleStore`, on the projects database."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy role store.

        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.role_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.role_store", immediate=True
        )
        # Test seam: runs between the caller's read of the row and the write,
        # outside any transaction, so a test can line up two writers at one generation.
        self.before_write_hook: Callable[[str], None] | None = None
        # Test seam: runs inside the CAS transaction, before its event is inserted.
        self.before_event_hook: Callable[[str], None] | None = None

    def _before_write(self, operation: str) -> None:
        if self.before_write_hook is not None:
            self.before_write_hook(operation)

    def _owned(self, *, user_id: str | None) -> Select[tuple[SqlRole]]:
        return select(SqlRole).where(
            SqlRole.workspace_id == current_workspace_id(),
            SqlRole.owner_key == (user_id or ""),
        )

    def get(self, role_id: str, *, user_id: str | None) -> Role | None:
        """Return the owner's role by id, else ``None``."""
        with self._session("select_role") as session:
            row = session.scalar(self._owned(user_id=user_id).where(SqlRole.id == role_id))
            return _to_entity(row) if row is not None else None

    def find(self, name: str, *, user_id: str | None, project_id: str | None) -> Role | None:
        """Return the role named ``name`` (any case) in one namespace, else ``None``."""
        with self._session("find_role_by_name") as session:
            row = session.scalar(
                self._owned(user_id=user_id).where(
                    SqlRole.project_key == _project_key(project_id),
                    SqlRole.name_key == name.casefold(),
                )
            )
            return _to_entity(row) if row is not None else None

    def list_namespace(
        self, *, user_id: str | None, project_id: str | None
    ) -> builtins.list[Role]:
        """List one namespace's roles, by name."""
        with self._session("list_namespace_roles") as session:
            rows = session.scalars(
                self._owned(user_id=user_id)
                .where(SqlRole.project_key == _project_key(project_id))
                .order_by(asc(SqlRole.name_key))
            ).all()
            return [_to_entity(row) for row in rows]

    def list_owned(self, *, user_id: str | None) -> builtins.list[Role]:
        """List every role the owner has, in any namespace."""
        with self._session("list_owned_roles") as session:
            rows = session.scalars(
                self._owned(user_id=user_id).order_by(asc(SqlRole.name_key), asc(SqlRole.id))
            ).all()
            return [_to_entity(row) for row in rows]

    def held_by(self, session_ids: Iterable[str]) -> builtins.list[Role]:
        """List the roles these sessions hold, in any namespace."""
        roles: list[Role] = []
        for chunk in _chunks(dict.fromkeys(session_ids), _IN_CHUNK):
            with self._session("select_roles_by_holder") as session:
                rows = session.scalars(
                    select(SqlRole).where(
                        SqlRole.workspace_id == current_workspace_id(),
                        SqlRole.holder_session_id.in_(chunk),
                    )
                ).all()
                roles.extend(_to_entity(row) for row in rows)
        return roles

    def _require_live_project(self, session: Session, project_id: str | None) -> None:
        """Share-lock the namespace's project until commit, like a session filing.

        ``roles.project_id`` has no foreign key, so this lock is what stops a
        concurrent project delete, whose release ran already, from leaving a held
        role in a project that is gone.
        """
        if project_id is None:
            return
        live = session.execute(
            select(SqlProject.id)
            .where(
                SqlProject.workspace_id == current_workspace_id(),
                SqlProject.id == project_id,
            )
            .with_for_update(read=True)
        ).scalar()
        if live is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

    def _swap(self, session: Session, role: Role, **values: object) -> bool:
        """The CAS: write ``values`` only if the row is still at ``role.generation``."""
        swapped = session.execute(
            update(SqlRole)
            .where(
                SqlRole.workspace_id == current_workspace_id(),
                SqlRole.id == role.id,
                SqlRole.generation == role.generation,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return cast("CursorResult[object]", swapped).rowcount > 0

    def _add_event(
        self,
        session: Session,
        role: Role,
        *,
        generation: int,
        kind: str,
        session_id: str | None,
        prev_session_id: str | None,
        actor: str,
        reason: str | None,
        role_path: str,
        at: int,
    ) -> None:
        if self.before_event_hook is not None:
            self.before_event_hook(kind)
        session.add(
            SqlRoleEvent(
                id=uuid.uuid4().hex,
                role_id=role.id,
                generation=generation,
                kind=encode_role_event_kind(kind),
                session_id=session_id,
                prev_session_id=prev_session_id,
                actor=encode_role_event_actor(actor),
                reason=reason,
                at=at,
                role_name=role.name,
                role_path=role_path,
            )
        )

    def create(
        self,
        name: str,
        *,
        user_id: str | None,
        project_id: str | None,
        overlay_ref: str | None = None,
        holder_session_id: str | None = None,
        actor: str = "system",
        reason: str | None = None,
        role_path: str = "",
    ) -> Role:
        """Insert a role, vacant unless ``holder_session_id`` adopts a label holder."""
        now = now_epoch()
        adopted = holder_session_id is not None
        role = Role(
            id=uuid.uuid4().hex,
            user_id=user_id,
            project_id=project_id,
            name=name,
            name_key=name.casefold(),
            overlay_ref=_stored_overlay(name, overlay_ref),
            defaults=None,
            permanent=False,
            holder_session_id=holder_session_id,
            previous_session_id=None,
            held_since=now if adopted else None,
            created_at=now,
            updated_at=None,
            generation=1 if adopted else 0,
        )

        def write(session: Session) -> Role:
            require_active_account(session, user_id)
            self._require_live_project(session, project_id)
            session.add(
                SqlRole(
                    id=role.id,
                    user_id=user_id,
                    project_id=project_id,
                    name=role.name,
                    name_key=role.name_key,
                    owner_key=user_id or "",
                    project_key=_project_key(project_id),
                    overlay_ref=role.overlay_ref,
                    holder_session_id=holder_session_id,
                    held_since=role.held_since,
                    created_at=now,
                    generation=role.generation,
                )
            )
            if adopted:
                self._add_event(
                    session,
                    role,
                    generation=1,
                    kind="adopt",
                    session_id=holder_session_id,
                    prev_session_id=None,
                    actor=actor,
                    reason=reason,
                    role_path=role_path,
                    at=now,
                )
            return role

        self._before_write("create")
        try:
            return run_write_transaction(self._session_immediate, "create_role", write)
        except IntegrityError as exc:
            raise OmnigentError(
                f"Role {name!r} already exists in this namespace",
                code=ErrorCode.ALREADY_EXISTS,
            ) from exc

    def take(
        self,
        role: Role,
        session_id: str,
        *,
        kind: str,
        actor: str,
        reason: str | None,
        role_path: str,
        overlay_ref: str | None,
    ) -> Role | None:
        """Make ``session_id`` the holder if ``role.generation`` is still current."""
        now = now_epoch()
        generation = role.generation + 1
        previous = role.holder_session_id or role.previous_session_id

        def write(session: Session) -> Role | None:
            require_active_account(session, role.user_id)
            self._require_live_project(session, role.project_id)
            if not self._swap(
                session,
                role,
                holder_session_id=session_id,
                previous_session_id=previous,
                held_since=now,
                updated_at=now,
                generation=generation,
                overlay_ref=_stored_overlay(role.name, overlay_ref),
            ):
                return None
            self._add_event(
                session,
                role,
                generation=generation,
                kind=kind,
                session_id=session_id,
                prev_session_id=role.holder_session_id,
                actor=actor,
                reason=reason,
                role_path=role_path,
                at=now,
            )
            row = session.get(SqlRole, (current_workspace_id(), role.id), populate_existing=True)
            return _to_entity(row) if row is not None else None

        self._before_write("take")
        try:
            return run_write_transaction(self._session_immediate, "take_role", write)
        except IntegrityError as exc:
            raise OmnigentError(
                f"Session {session_id} already holds a role; release it first",
                code=ErrorCode.CONFLICT,
            ) from exc

    def release(
        self,
        role: Role,
        *,
        actor: str,
        reason: str | None,
        role_path: str,
    ) -> Role | None:
        """Vacate ``role`` if ``role.generation`` is still current."""
        now = now_epoch()
        generation = role.generation + 1

        def write(session: Session) -> Role | None:
            if not self._swap(
                session,
                role,
                holder_session_id=None,
                previous_session_id=role.holder_session_id,
                held_since=None,
                updated_at=now,
                generation=generation,
            ):
                return None
            self._add_event(
                session,
                role,
                generation=generation,
                kind="release",
                session_id=role.holder_session_id,
                prev_session_id=role.holder_session_id,
                actor=actor,
                reason=reason,
                role_path=role_path,
                at=now,
            )
            row = session.get(SqlRole, (current_workspace_id(), role.id), populate_existing=True)
            return _to_entity(row) if row is not None else None

        self._before_write("release")
        return run_write_transaction(self._session_immediate, "release_role", write)

    def set_permanent(self, role: Role, permanent: bool) -> Role | None:
        """Set the permanent flag if ``role.generation`` is still current."""

        def write(session: Session) -> Role | None:
            if not self._swap(
                session,
                role,
                permanent=permanent,
                updated_at=now_epoch(),
                generation=role.generation + 1,
            ):
                return None
            row = session.get(SqlRole, (current_workspace_id(), role.id), populate_existing=True)
            return _to_entity(row) if row is not None else None

        self._before_write("set_permanent")
        return run_write_transaction(self._session_immediate, "set_role_permanent", write)

    def events(self, role_id: str) -> builtins.list[RoleEvent]:
        """List one role's events, oldest first."""
        with self._session("list_role_events") as session:
            rows = session.scalars(
                select(SqlRoleEvent)
                .where(
                    SqlRoleEvent.workspace_id == current_workspace_id(),
                    SqlRoleEvent.role_id == role_id,
                )
                .order_by(asc(SqlRoleEvent.generation), asc(SqlRoleEvent.at))
            ).all()
            return [_event_entity(row) for row in rows]

    def _lanes(self) -> Select[tuple[SqlRoleLane]]:
        return select(SqlRoleLane).where(SqlRoleLane.workspace_id == current_workspace_id())

    def claim_lane(
        self,
        role_id: str,
        name: str,
        session_id: str,
        *,
        purpose: str | None,
        item: str | None,
    ) -> RoleLane:
        """Insert an open lane; the unique indexes decide a race."""
        lane = RoleLane(
            id=uuid.uuid4().hex,
            role_id=role_id,
            name=name,
            session_id=session_id,
            purpose=purpose,
            item=item,
            created_at=now_epoch(),
        )

        def write(session: Session) -> RoleLane:
            session.add(
                SqlRoleLane(
                    id=lane.id,
                    role_id=role_id,
                    name=name,
                    session_id=session_id,
                    purpose=purpose,
                    item=item,
                    created_at=lane.created_at,
                )
            )
            return lane

        self._before_write("claim_lane")
        try:
            return run_write_transaction(self._session_immediate, "claim_role_lane", write)
        except IntegrityError as exc:
            held = self.find_lane(role_id, name)
            if held is not None:
                message = f"Lane {name!r} is taken by session {held.session_id}"
            else:
                current = self.lane_of(session_id)
                where = f" ({current.name!r})" if current is not None else ""
                message = f"Session {session_id} already works a lane{where}; close it first"
            raise OmnigentError(message, code=ErrorCode.CONFLICT) from exc

    def find_lane(self, role_id: str, name: str) -> RoleLane | None:
        """Return the role's open lane named ``name``, else ``None``."""
        with self._session("find_role_lane") as session:
            row = session.scalar(
                self._lanes().where(SqlRoleLane.role_id == role_id, SqlRoleLane.name == name)
            )
            return _lane_entity(row) if row is not None else None

    def lane_of(self, session_id: str) -> RoleLane | None:
        """Return the lane ``session_id`` works, else ``None``."""
        with self._session("select_lane_by_session") as session:
            row = session.scalar(self._lanes().where(SqlRoleLane.session_id == session_id))
            return _lane_entity(row) if row is not None else None

    def lanes_of_roles(self, role_ids: Iterable[str]) -> builtins.list[RoleLane]:
        """List the open lanes of these roles, by role then name."""
        lanes: list[RoleLane] = []
        for chunk in _chunks(dict.fromkeys(role_ids), _IN_CHUNK):
            with self._session("list_role_lanes") as session:
                rows = session.scalars(
                    self._lanes()
                    .where(SqlRoleLane.role_id.in_(chunk))
                    .order_by(asc(SqlRoleLane.role_id), asc(SqlRoleLane.name))
                ).all()
                lanes.extend(_lane_entity(row) for row in rows)
        return lanes

    def close_lane(self, lane_id: str) -> bool:
        """Delete a lane, freeing its name; ``False`` when it was already gone."""

        def write(session: Session) -> bool:
            deleted = session.execute(
                delete(SqlRoleLane)
                .where(
                    SqlRoleLane.workspace_id == current_workspace_id(),
                    SqlRoleLane.id == lane_id,
                )
                .execution_options(synchronize_session=False)
            )
            return cast("CursorResult[object]", deleted).rowcount > 0

        return run_write_transaction(self._session_immediate, "close_role_lane", write)

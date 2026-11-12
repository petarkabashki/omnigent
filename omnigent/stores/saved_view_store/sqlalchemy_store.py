"""SQLAlchemy-backed saved view store, on the projects database.

Every read and write is scoped to the owner. The store keeps specs as given; callers
validate them first (see :mod:`omnigent.views.saved`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlSavedView, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.saved_view import SavedView
from omnigent.errors import ErrorCode, OmnigentError

_UNSET: Any = object()


def _view(row: SqlSavedView) -> SavedView:
    return SavedView(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        description=row.description or "",
        spec=json.loads(row.spec),
        params=json.loads(row.params) if row.params else {},
        project_id=row.project_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _name_taken(name: str) -> OmnigentError:
    return OmnigentError(f"a view named {name!r} already exists", code=ErrorCode.ALREADY_EXISTS)


class SqlAlchemySavedViewStore:
    """Persists saved views; every query is scoped to the owner."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store.

        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.saved_view_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.saved_view_store", immediate=True
        )

    def _owned(self, user_id: str | None) -> Select[tuple[SqlSavedView]]:
        return select(SqlSavedView).where(
            SqlSavedView.workspace_id == current_workspace_id(),
            SqlSavedView.owner_key == (user_id or ""),
        )

    def _name_in_use(self, session: Session, name: str, user_id: str | None, own_id: str) -> bool:
        existing = session.scalar(
            self._owned(user_id).where(SqlSavedView.name == name, SqlSavedView.id != own_id)
        )
        return existing is not None

    def create(
        self,
        *,
        name: str,
        spec: dict[str, Any],
        params: dict[str, Any] | None = None,
        description: str = "",
        project_id: str | None = None,
        user_id: str | None,
    ) -> SavedView:
        """
        Insert a view owned by ``user_id``.

        :raises OmnigentError: ``ALREADY_EXISTS`` when the owner has a view of that name.
        """
        view_id = uuid.uuid4().hex
        now = now_epoch()

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            if self._name_in_use(session, name, user_id, view_id):
                raise _name_taken(name)
            session.add(
                SqlSavedView(
                    id=view_id,
                    user_id=user_id,
                    owner_key=user_id or "",
                    name=name,
                    description=description,
                    spec=_dumps(spec),
                    params=_dumps(params or {}),
                    project_id=project_id,
                    created_at=now,
                )
            )

        try:
            run_write_transaction(self._session_immediate, "create_saved_view", write)
        except IntegrityError as error:
            raise _name_taken(name) from error
        created = self.get(view_id, user_id=user_id)
        assert created is not None
        return created

    def get(self, view_id: str, *, user_id: str | None) -> SavedView | None:
        """The owner's view, else ``None``."""
        with self._session("select_saved_view") as session:
            row = session.scalar(self._owned(user_id).where(SqlSavedView.id == view_id))
            return _view(row) if row is not None else None

    def get_by_name(self, name: str, *, user_id: str | None) -> SavedView | None:
        """The owner's view of that name, else ``None``."""
        with self._session("select_saved_view_by_name") as session:
            row = session.scalar(self._owned(user_id).where(SqlSavedView.name == name))
            return _view(row) if row is not None else None

    def list_owned(self, *, user_id: str | None, project_id: str | None = None) -> list[SavedView]:
        """The owner's views by name, optionally only those filed under ``project_id``."""
        query = self._owned(user_id)
        if project_id is not None:
            query = query.where(SqlSavedView.project_id == project_id)
        with self._session("list_saved_views") as session:
            return [_view(row) for row in session.scalars(query.order_by(SqlSavedView.name))]

    def update(
        self,
        view_id: str,
        *,
        user_id: str | None,
        name: str = _UNSET,
        description: str = _UNSET,
        spec: dict[str, Any] = _UNSET,
        params: dict[str, Any] = _UNSET,
        project_id: str | None = _UNSET,
    ) -> SavedView | None:
        """
        Change the given fields of the owner's view; ``None`` when it is not theirs.

        :raises OmnigentError: ``ALREADY_EXISTS`` when renaming onto another view's name.
        """

        def write(session: Session) -> bool:
            row = session.scalar(self._owned(user_id).where(SqlSavedView.id == view_id))
            if row is None:
                return False
            if name is not _UNSET and name != row.name:
                if self._name_in_use(session, name, user_id, view_id):
                    raise _name_taken(name)
                row.name = name
            if description is not _UNSET:
                row.description = description
            if spec is not _UNSET:
                row.spec = _dumps(spec)
            if params is not _UNSET:
                row.params = _dumps(params)
            if project_id is not _UNSET:
                row.project_id = project_id
            row.updated_at = now_epoch()
            return True

        try:
            found = run_write_transaction(self._session_immediate, "update_saved_view", write)
        except IntegrityError as error:
            raise _name_taken(str(name)) from error
        return self.get(view_id, user_id=user_id) if found else None

    def delete(self, view_id: str, *, user_id: str | None) -> bool:
        """Delete the owner's view; ``False`` when it is not theirs."""

        def write(session: Session) -> bool:
            row = session.scalar(self._owned(user_id).where(SqlSavedView.id == view_id))
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_saved_view", write)

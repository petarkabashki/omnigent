"""Role store — persists role rows and their append-only event history.

A role names one addressable seat in an owner's namespace. The row decides who
holds it; session labels are mirrors written by the server. Whether a take is
allowed is decided by the caller from the session's state, which can live on
another database, so this store only offers the compare-and-swap that makes the
decision safe: every write names the generation it was decided at.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Iterable

from omnigent.entities import Role, RoleEvent


class RoleStore(ABC):
    """Abstract base for role persistence, scoped by owner on every read."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the role store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get(self, role_id: str, *, user_id: str | None) -> Role | None:
        """Return the owner's role by id, else ``None``."""

    @abstractmethod
    def find(self, name: str, *, user_id: str | None, project_id: str | None) -> Role | None:
        """Return the role named ``name`` (any case) in one namespace, else ``None``."""

    @abstractmethod
    def list_namespace(
        self, *, user_id: str | None, project_id: str | None
    ) -> builtins.list[Role]:
        """List one namespace's roles, by name."""

    @abstractmethod
    def list_owned(self, *, user_id: str | None) -> builtins.list[Role]:
        """List every role the owner has, in any namespace."""

    @abstractmethod
    def held_by(self, session_ids: Iterable[str]) -> builtins.list[Role]:
        """List the roles these sessions hold, in any namespace."""

    @abstractmethod
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
        """
        Insert a role, vacant unless ``holder_session_id`` adopts a label holder.

        :param name: The role name in display case.
        :param user_id: The owner, or ``None`` in single-user mode.
        :param project_id: The namespace's project, or ``None`` for the root.
        :param overlay_ref: Role-file name; one equal to ``name`` is stored as ``None``.
        :param holder_session_id: A session adopted as holder, which also writes
            an ``adopt`` event at generation 1.
        :param actor: The event's actor when adopting.
        :param reason: The event's reason when adopting.
        :param role_path: The namespace path snapshotted on the event.
        :returns: The new role.
        :raises OmnigentError: ``ALREADY_EXISTS`` when the unique name index or
            holder index rejects it; ``NOT_FOUND`` when the project is gone.
        """

    @abstractmethod
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
        """
        Make ``session_id`` the holder if ``role.generation`` is still current.

        The CAS and its event commit together.

        :param role: The row as read when the take was decided.
        :param session_id: The new holder.
        :param kind: The event kind, ``take`` or ``stale_repair``.
        :param actor: The event's actor.
        :param reason: The event's reason.
        :param role_path: The namespace path snapshotted on the event.
        :param overlay_ref: The overlay the row keeps; one equal to the name is
            stored as ``None``.
        :returns: The updated role, or ``None`` when the race was lost.
        :raises OmnigentError: ``CONFLICT`` when the session already holds a
            role; ``NOT_FOUND`` when the project is gone.
        """

    @abstractmethod
    def release(
        self,
        role: Role,
        *,
        actor: str,
        reason: str | None,
        role_path: str,
    ) -> Role | None:
        """
        Vacate ``role`` if ``role.generation`` is still current.

        :param role: The row as read when the release was decided.
        :param actor: The event's actor.
        :param reason: The event's reason, e.g. ``"archive"``.
        :param role_path: The namespace path snapshotted on the event.
        :returns: The updated role, or ``None`` when the row moved on.
        """

    @abstractmethod
    def events(self, role_id: str) -> builtins.list[RoleEvent]:
        """List one role's events, oldest first."""

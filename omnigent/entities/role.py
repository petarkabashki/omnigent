"""Role entities — persisted in the ``roles`` and ``role_events`` tables.

A :class:`Role` is one addressable name inside an owner's namespace. The row is
the source of truth for who holds the name; the ``omnigent.alias``,
``omnigent.role_id`` and ``omnigent.role_overlay`` session labels mirror it.
Rows stay after a release, so a later take of the same name keeps the ``id``.
:class:`RoleEvent` rows are an append-only history of tenures, identified by
``(role_id, generation)``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Role:
    """
    A role persisted in the ``roles`` table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param user_id: Owner, copied from the project. ``None`` in single-user mode.
    :param project_id: The namespace's project, or ``None`` for the owner's
        unfiled root.
    :param name: Role name in display case, e.g. ``"Architect"``.
    :param name_key: Case-folded ``name``; what uniqueness is decided on.
    :param overlay_ref: Role-file name when it differs from ``name``, else ``None``.
    :param defaults: Opaque pre-fill JSON text, or ``None``.
    :param permanent: Whether the role outlives its holders by design.
    :param holder_session_id: Current holder, or ``None`` when vacant.
    :param previous_session_id: The last holder before the current one.
    :param held_since: Unix epoch seconds of the current tenure, or ``None``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    :param generation: Bumped on every take and release; the CAS token.
    """

    id: str
    user_id: str | None
    project_id: str | None
    name: str
    name_key: str
    overlay_ref: str | None
    defaults: str | None
    permanent: bool
    holder_session_id: str | None
    previous_session_id: str | None
    held_since: int | None
    created_at: int
    updated_at: int | None
    generation: int


@dataclass
class RoleEvent:
    """
    One row of a role's append-only history.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param role_id: The role this event belongs to.
    :param generation: The role's generation after this event.
    :param kind: ``take``, ``release``, ``takeover``, ``adopt``,
        ``stale_repair`` or ``moved``.
    :param session_id: The session the event acted on, if any.
    :param prev_session_id: The session that held the role before, if any.
    :param actor: ``owner``, ``agent``, ``bootstrap`` or ``system``.
    :param reason: Why the event happened, e.g. ``"archive"`` or ``"label"``.
    :param at: Unix epoch seconds of the event.
    :param role_name: The role's name when the event was written.
    :param role_path: The role's namespace path when the event was written
        (``""`` for the unfiled root).
    """

    id: str
    role_id: str
    generation: int
    kind: str
    session_id: str | None
    prev_session_id: str | None
    actor: str
    reason: str | None
    at: int
    role_name: str
    role_path: str


@dataclass
class RoleLane:
    """
    A named lane of a role, worked by one session: ``<project>/<Alias>/<lane>``.

    The row belongs to the role, not to its holder, so a release or takeover
    leaves it in place. It is deleted when its session closes.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param role_id: The role the lane works for.
    :param name: Slug unique within the role, e.g. ``"c30"``.
    :param session_id: The top-level session working the lane.
    :param purpose: One line on what the lane is for, or ``None``.
    :param item: Roadmap item id, e.g. ``"C30"``, or ``None``.
    :param created_at: Unix epoch seconds at claim.
    """

    id: str
    role_id: str
    name: str
    session_id: str
    purpose: str | None
    item: str | None
    created_at: int

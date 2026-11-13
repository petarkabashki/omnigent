"""Project store — persists first-class, owner-private projects.

A project is a user-defined container that groups sessions and exists
independently of its members (see ``designs/PROJECTS_PRD.md``). This store owns
the ``projects`` table. Session→project membership lives on the conversation's
metadata row (``project_id``) and is managed by the conversation store, not
here.

Projects have no ACL of their own (PRD §9): every method is scoped by
``user_id`` so a caller only ever sees and mutates their own projects.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict, TypeVar

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore, ProjectSessionSweep
    from omnigent.stores.group_thread_store import SqlAlchemyGroupThreadStore
    from omnigent.stores.role_store import RoleStore
    from omnigent.stores.saved_view_store import SqlAlchemySavedViewStore

# Deepest allowed namespace path (segments), cycle-guard bound for path walks.
MAX_PROJECT_DEPTH: Final = 8


class _Unset:
    """Type of :data:`UNSET`, the "leave unchanged" marker for nullable fields."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


def validate_project_segment(name: str) -> str:
    """Return ``name`` if it is a valid new path segment, else raise ``INVALID_INPUT``.

    A segment may not contain ``/`` and may not be ``.`` or ``..``. Existing rows
    keep legacy names; this guards new and renamed ones.
    """
    if "/" in name:
        raise OmnigentError("Project names cannot contain '/'", code=ErrorCode.INVALID_INPUT)
    if name in (".", ".."):
        raise OmnigentError(f"Project name {name!r} is reserved", code=ErrorCode.INVALID_INPUT)
    return name


def project_path(project: Project, by_id: dict[str, Project]) -> str:
    """Compute the ``/``-joined namespace path by walking ``parent_id``.

    The walk stops at a missing parent (treated as a root), at a cycle, and at
    :data:`MAX_PROJECT_DEPTH` segments.
    """
    segments = [project.name]
    seen = {project.id}
    current = project
    while current.parent_id is not None and len(segments) < MAX_PROJECT_DEPTH:
        parent = by_id.get(current.parent_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        segments.append(parent.name)
        current = parent
    return "/".join(reversed(segments))


def effective_private_ids(parents: dict[str, str | None], flagged: set[str]) -> set[str]:
    """Return the ids in ``parents`` that are flagged or have a flagged ancestor.

    The walk stops at a missing parent, at a cycle, and at :data:`MAX_PROJECT_DEPTH`.
    """
    result: set[str] = set()
    for project_id in parents:
        current: str | None = project_id
        seen: set[str] = set()
        while current is not None and current not in seen and len(seen) < MAX_PROJECT_DEPTH:
            if current in flagged:
                result.add(project_id)
                break
            seen.add(current)
            current = parents.get(current)
    return result


def walk_project_path(projects: list[Project], segments: list[str]) -> list[Project]:
    """Tree-walk ``segments`` from the roots, matching each segment case-insensitively.

    :returns: The projects at the end of the path: empty when nothing matches,
        more than one only for siblings that differ by case alone.
    """
    known = {p.id for p in projects}
    # A project whose parent row is missing counts as a root, as in project_path.
    level = [p for p in projects if p.parent_id is None or p.parent_id not in known]
    for index, segment in enumerate(segments):
        key = segment.casefold()
        hits = [p for p in level if p.name.casefold() == key]
        if not hits or index == len(segments) - 1:
            return hits
        parent_ids = {p.id for p in hits}
        level = [p for p in projects if p.parent_id in parent_ids]
    return []


def lookup_project_path(projects: list[Project], path: str) -> tuple[list[Project], list[Project]]:
    """Resolve ``path`` by tree walk and, separately, as a legacy flat root name.

    :returns: ``(tree, flat)``. ``flat`` holds root projects whose whole name is
        ``path`` (legacy ``a/b`` names) and is only searched when ``path`` has a ``/``.
    """
    tree = walk_project_path(projects, [segment.strip() for segment in path.split("/")])
    flat = [p for p in projects if "/" in path and p.parent_id is None and p.name == path]
    return tree, flat


@dataclass(frozen=True)
class SlashMigrationStep:
    """One proposed conversion of a legacy ``a/b`` root project into a tree node.

    :param project_id: The legacy project.
    :param old_name: Its current name (contains ``/``).
    :param create: Namespace paths to create first (ancestors that do not exist yet).
    :param parent_path: Path of the new parent ("" when the project stays a root).
    :param new_name: The last segment, the project's new name.
    :param problem: Why the step cannot be applied as proposed, else ``None``.
    """

    project_id: str
    old_name: str
    create: tuple[str, ...]
    parent_path: str
    new_name: str
    problem: str | None = None


def plan_slash_migration(projects: list[Project]) -> list[SlashMigrationStep]:
    """Propose how to turn root projects named ``a/b`` into a tree (never applied here).

    Ancestors that already exist as tree nodes are reused; a step is flagged when a
    segment is empty, ``.`` or ``..``, the path is too deep, or the target path is taken.
    """
    by_id = {p.id: p for p in projects}
    taken = {project_path(p, by_id).casefold(): p for p in projects if "/" not in p.name}
    steps: list[SlashMigrationStep] = []
    for project in sorted(projects, key=lambda p: p.name):
        if project.parent_id is not None or "/" not in project.name:
            continue
        segments = project.name.split("/")
        problem = None
        if any(not s.strip() or s in (".", "..") for s in segments):
            problem = "has an empty, '.' or '..' segment"
        elif len(segments) > MAX_PROJECT_DEPTH:
            problem = f"is deeper than {MAX_PROJECT_DEPTH} levels"
        elif (clash := taken.get(project.name.casefold())) is not None:
            problem = f"target path is already project {clash.id}"
        parents = ["/".join(segments[:i]) for i in range(1, len(segments))]
        create = tuple(p for p in parents if p.casefold() not in taken)
        if problem is None:
            for path in (*create, project.name):
                taken[path.casefold()] = project
        steps.append(
            SlashMigrationStep(
                project_id=project.id,
                old_name=project.name,
                create=create,
                parent_path="/".join(segments[:-1]),
                new_name=segments[-1],
                problem=problem,
            )
        )
    return steps


class ProjectOrderPreference(TypedDict):
    sort_mode: Literal["alphabetical", "manual"]
    ordered_project_ids: list[str] | None


class ProjectStore(ABC):
    """
    Abstract base for project persistence.

    Manages the lifecycle of projects (CRUD). All reads and writes are scoped
    by ``user_id`` because projects are owner-private.

    :cvar roles: The role store on the same database, or ``None`` when this
        backend has no role rows and aliases stay plain labels.
    :cvar group_threads: The group thread store on the same database, or ``None``.
    :cvar saved_views: The saved view store on the same database, or ``None``.
    """

    roles: RoleStore | None = None
    group_threads: SqlAlchemyGroupThreadStore | None = None
    saved_views: SqlAlchemySavedViewStore | None = None

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the project store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Project:
        """
        Insert a new, empty project.

        :param project_id: Pre-generated unique project id (a UUID string).
        :param name: Human-readable project name. Trimmed, non-empty, unique
            among the owner's projects.
        :param user_id: Owning user, or ``None`` in single-user mode.
        :param config: Optional default session settings (opaque JSON object);
            ``None`` or empty stores no defaults.
        :param parent_id: Parent project to nest under, or ``None`` for a root.
        :returns: The newly created :class:`Project`.
        :raises OmnigentError: ``ALREADY_EXISTS`` if a sibling already has this
            name; ``INVALID_INPUT`` for a ``/`` or reserved name, a missing
            parent, or a path deeper than :data:`MAX_PROJECT_DEPTH`.
        """
        ...

    @abstractmethod
    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """
        Return an owned project by id, or ``None`` if not found.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner; a project owned by someone
            else is treated as not found.
        :returns: The :class:`Project` if found and owned, else ``None``.
        """
        ...

    @abstractmethod
    def list(self, *, user_id: str | None) -> list[Project]:
        """
        List the owner's projects ordered by ``created_at ASC, id ASC``.

        :param user_id: The owner whose projects to return.
        :returns: List of :class:`Project` instances.
        """
        ...

    @abstractmethod
    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        parent_id: str | _Unset | None = UNSET,
    ) -> Project | None:
        """
        Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param name: New name, or ``None`` to leave unchanged. Trimmed,
            non-empty, unique among the owner's projects.
        :param config: New config object to replace the stored one, or ``None``
            to leave it unchanged. An empty dict clears the stored defaults.
        :param parent_id: New parent (a move); ``None`` moves to the root and
            :data:`UNSET` leaves the parent unchanged.
        :returns: The updated :class:`Project`, or ``None`` if not found.
        :raises OmnigentError: ``ALREADY_EXISTS`` if the new name/parent collides
            with a sibling; ``INVALID_INPUT`` for a ``/`` or reserved name, a
            missing parent, a cycle, or a subtree that would exceed
            :data:`MAX_PROJECT_DEPTH`.
        """
        ...

    @abstractmethod
    def set_private(
        self, project_id: str, *, user_id: str | None, private: bool
    ) -> Project | None:
        """Set the owned project's private flag; the only write path for it.

        :returns: The updated project, or ``None`` if not found or not owned.
        """
        ...

    @abstractmethod
    def private_project_ids(self, *, user_id: str | None) -> set[str]:
        """Return owned project ids that are private themselves or through an ancestor."""
        ...

    @abstractmethod
    def get_path(self, project_id: str, *, user_id: str | None) -> str | None:
        """Return the owned project's namespace path, or ``None`` if not found."""
        ...

    @abstractmethod
    def find_by_path(self, path: str, *, user_id: str | None) -> builtins.list[Project]:
        """Resolve ``path`` over the owner's projects: tree walk first, then a legacy flat name.

        :returns: Matches at the end of the path: empty if none, several only for
            case-only sibling duplicates.
        :raises OmnigentError: ``CONFLICT`` when the tree walk and a different legacy
            flat project (whose whole name is ``path``) both match.
        """
        ...

    @abstractmethod
    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """
        Delete an owned project. Idempotent.

        Deleting a project does not delete its member sessions; unfiling them
        (clearing ``project_id``) is the caller's responsibility.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :returns: ``True`` if removed; ``False`` if not found / not owned.
        :raises OmnigentError: ``CONFLICT`` while the project still has children.
        """
        ...

    @abstractmethod
    def member_session_ids(
        self,
        project_id: str,
        *,
        user_id: str | None,
        conversation_store: ConversationStore,
    ) -> builtins.list[str]:
        """
        Read a project's members without locking, for checks ahead of a delete.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param conversation_store: The store holding membership.
        :returns: Member session ids; empty if not found / not owned.
        """
        ...

    @abstractmethod
    def delete_with_sessions(
        self,
        project_id: str,
        *,
        user_id: str | None,
        conversation_store: ConversationStore,
        authorize: Callable[[builtins.list[str]], None],
    ) -> ProjectSessionSweep | None:
        """
        Archive and unfile a project's members and delete it in one transaction.

        Any refusal or failure leaves the members and the project untouched.

        :param project_id: Opaque project identifier.
        :param user_id: The requesting owner.
        :param conversation_store: The store holding membership.
        :param authorize: Raises to refuse; receives every member id. It may run
            more than once, under the write lock, so keep it cheap for known ids.
        :returns: What was swept; ``None`` if not found / not owned.
        :raises OmnigentError: ``CONFLICT`` while the project has children, or
            has members in a separate database.
        """
        ...

    @abstractmethod
    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Read saved IDs; None means alphabetical order."""
        raise NotImplementedError

    @abstractmethod
    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read the sorting mode and remembered manual order."""
        raise NotImplementedError

    @abstractmethod
    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Save owned IDs; unranked projects append. Null retains IDs in alphabetical mode."""
        raise NotImplementedError


_T = TypeVar("_T")


def apply_project_order(
    projects: list[_T],
    order: list[str] | None,
    *,
    project_id: Callable[[_T], str | None],
    project_name: Callable[[_T], str],
) -> list[_T]:
    """Order projects, retaining input order for unranked entries in custom mode."""
    if order is None:
        return sorted(projects, key=project_name)
    by_id = {
        project_id(project): project for project in projects if project_id(project) is not None
    }
    ranked = dict.fromkeys(order)
    return [by_id[id] for id in ranked if id in by_id] + [
        project for project in projects if project_id(project) not in ranked
    ]

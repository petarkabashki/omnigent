"""Role aliases that address top-level sessions as ``Role``, ``/Role`` or ``path/Role``.

A row in the role store decides who holds a name; the ``omnigent.alias``,
``omnigent.role_id`` and ``omnigent.role_overlay`` labels mirror it for readers
on the runner. The two can live on different databases, so writes are ordered to
fail safe and resolve repairs labels: a take writes the row first, a release
writes the labels first, and on any drift the row wins.

A name with no row falls back to the older label scan, which writes nothing.
Legacy label-projects have no rows at all, so their aliases stay plain labels.
"""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from omnigent.entities import Conversation, Role
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.session_alias_client import ALIAS_PATTERN
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ROLE_ID_LABEL_KEY,
    ROLE_OVERLAY_LABEL_KEY,
    ROLE_RELEASED_AT_LABEL_KEY,
    ROLE_RELEASED_BY_LABEL_KEY,
    ROLE_RELEASED_LABEL_KEY,
    ROLE_RELEASED_LABEL_KEYS,
    ROLE_RELEASED_TO_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.project_store import ProjectStore, lookup_project_path
from omnigent.stores.role_store import RoleStore
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.policies.builtins.messaging import HandleResolver

_logger = logging.getLogger(__name__)

_PAGE_SIZE = 200
_MAX_PAGES = 50
_MIRROR_LABEL_KEYS = (ALIAS_LABEL_KEY, ROLE_ID_LABEL_KEY, ROLE_OVERLAY_LABEL_KEY)
_RELEASE_CAS_ATTEMPTS = 3
_RELEASE_HOOK_ATTEMPTS = 3
_RELEASE_HOOK_BACKOFF_S = 0.05


def validate_alias(alias: object) -> str:
    """Return a trimmed alias or raise ``INVALID_INPUT``."""
    value = alias.strip() if isinstance(alias, str) else ""
    if not ALIAS_PATTERN.match(value):
        raise OmnigentError(
            "Alias must start with a letter and use 1–32 letters, digits, '-' or '_'",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


def validate_overlay_ref(overlay_ref: object) -> str | None:
    """Return a trimmed role-file name, or raise ``INVALID_INPUT``; ``None`` passes through."""
    if overlay_ref is None:
        return None
    value = overlay_ref.strip() if isinstance(overlay_ref, str) else ""
    if not ALIAS_PATTERN.match(value):
        raise OmnigentError(
            "Role overlay must start with a letter and use 1–32 letters, digits, '-' or '_'",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


@dataclass(frozen=True)
class Namespace:
    """The sessions one address prefix covers.

    :param path: Display path (``""`` = the owner's unfiled root).
    :param store_name: Project name handed to the conversation store's filter.
    :param project_ids: First-class projects whose members belong.
    :param label_name: Legacy ``omni_project`` label value that also belongs
        (only for sessions with no first-class project), or ``None``.
    :param exact: Whether to post-filter store results to ``project_ids`` and
        ``label_name`` (the store matches by name, which is ambiguous in a tree).
    """

    path: str
    store_name: str
    project_ids: frozenset[str] = frozenset()
    label_name: str | None = None
    exact: bool = False

    def contains(self, session: Conversation) -> bool:
        """Whether ``session`` belongs to this namespace (store results are a superset)."""
        if not self.exact or not self.path:
            return True
        project_id = getattr(session, "project_id", None)
        if project_id is not None:
            return project_id in self.project_ids
        return self.label_name is not None and (
            session.labels.get(PROJECT_LABEL_KEY) == self.label_name
        )


def session_project_name(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str:
    """Namespace path of the session's first-class project, else its legacy label, else ``""``."""
    if session.project_id and projects is not None:
        path = projects.get_path(session.project_id, user_id=user_id)
        if path is not None:
            return path
    return session.labels.get(PROJECT_LABEL_KEY, "")


def namespace_for_project(
    project_id: str, projects: ProjectStore, user_id: str | None
) -> Namespace | None:
    """The namespace of one first-class project, by id (no path re-parsing), else ``None``."""
    project = projects.get(project_id, user_id=user_id)
    path = projects.get_path(project_id, user_id=user_id) if project is not None else None
    if project is None or path is None:
        return None
    return Namespace(
        path,
        project.name,
        frozenset({project.id}),
        label_name=path if project.parent_id is None else None,
        exact=True,
    )


def namespace_for_session(
    session: Conversation | None, projects: ProjectStore | None, user_id: str | None
) -> Namespace:
    """The namespace a bare ``Role`` resolves in: the session's own project, else the root."""
    if session is None:
        return Namespace("", "")
    if session.project_id and projects is not None:
        namespace = namespace_for_project(session.project_id, projects, user_id)
        if namespace is not None:
            return namespace
    label = session.labels.get(PROJECT_LABEL_KEY, "")
    # A label also names the same-named root project, so its filed members share the namespace.
    roots = frozenset(
        project.id
        for project in (projects.list(user_id=user_id) if label and projects is not None else ())
        if project.parent_id is None and project.name == label
    )
    return Namespace(label, label, roots, label_name=label or None, exact=projects is not None)


def resolve_namespace(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    path: str,
    user_id: str | None,
) -> Namespace:
    """Resolve a namespace path: a tree walk first, then a flat legacy name or label-project.

    :raises OmnigentError: ``CONFLICT`` when the tree walk and a flat legacy
        project (or label-project) both match, or when sibling names differ by case only.
    """
    path = path.strip()
    if not path or projects is None:
        return Namespace(path, path)
    segments = path.split("/")
    if any(not segment.strip() for segment in segments):
        raise OmnigentError(f"Invalid namespace path {path!r}", code=ErrorCode.INVALID_INPUT)
    owned = projects.list(user_id=user_id)
    tree, flat = lookup_project_path(owned, path)
    if len(tree) > 1:
        ids = ", ".join(p.id for p in tree)
        raise OmnigentError(
            f"Namespace {path!r} is ambiguous: sibling projects {ids} differ only by case",
            code=ErrorCode.CONFLICT,
        )
    nested = "/" in path
    legacy = (
        f"legacy project {path!r} (id {flat[0].id})"
        if flat
        else f"label-project {path!r}"
        if tree and nested and path in conversations.list_projects(owned_by=user_id)
        else None
    )
    if tree and legacy is not None and (not flat or flat[0].id != tree[0].id):
        raise OmnigentError(
            f"{path!r} matches both project {path} (tree, id {tree[0].id}) and {legacy}. "
            "Run `omnigent projects migrate-slashes --dry-run`.",
            code=ErrorCode.CONFLICT,
        )
    if tree:
        return Namespace(
            path,
            tree[0].name,
            frozenset({tree[0].id}),
            label_name=None if nested else tree[0].name,
            exact=True,
        )
    return Namespace(
        path,
        path,
        frozenset(p.id for p in flat),
        label_name=path,
        exact=True,
    )


def role_handle(path: str, name: str) -> str:
    """``<path>/Role``; the unfiled root always shows a leading ``/``."""
    return f"{path}/{name}"


def session_handle(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str | None:
    """``<path>/Role`` (``/Role`` when unfiled) for an aliased session, else ``None``."""
    alias = session.labels.get(ALIAS_LABEL_KEY)
    if not alias:
        return None
    return role_handle(session_project_name(session, projects, user_id), alias)


def find_aliased_sessions(
    conversations: ConversationStore,
    *,
    user_id: str | None,
    project: str | None,
    alias: str | None = None,
    namespace: Namespace | None = None,
) -> list[Conversation]:
    """Live top-level aliased sessions the user owns.

    ``project`` is a project name (``""`` = unfiled, ``None`` = any project);
    ``alias`` matches case-insensitively (``None`` = any alias). A ``namespace``
    replaces ``project`` and narrows the store's name match to that exact tree node.
    """
    if namespace is not None:
        project = namespace.store_name
    wanted = alias.casefold() if alias else None
    found: list[Conversation] = []
    after: str | None = None
    for _ in range(_MAX_PAGES):
        page = conversations.list_conversations(
            limit=_PAGE_SIZE,
            after=after,
            kind="default",
            owned_by=user_id,
            project=project,
            has_label=ALIAS_LABEL_KEY,
        )
        for session in page.data:
            value = session.labels.get(ALIAS_LABEL_KEY, "")
            if not value or session.archived or is_session_closed(session.labels, session.title):
                continue
            if (wanted is None or value.casefold() == wanted) and (
                namespace is None or namespace.contains(session)
            ):
                found.append(session)
        if not page.has_more or not page.data:
            break
        after = page.data[-1].id
    return found


@dataclass(frozen=True)
class HolderStatus:
    """What the server knows about a role holder's runner.

    :param runner_online: Whether a runner tunnel is registered now; ``None``
        when unknown.
    :param runner_last_seen: Epoch seconds the runner was last seen alive.
    :param last_activity_at: Epoch seconds of the session's last update.
    """

    runner_online: bool | None = None
    runner_last_seen: int | None = None
    last_activity_at: int | None = None


HolderStatusLookup = Callable[[list[str]], dict[str, HolderStatus]]


def _role_store(projects: ProjectStore | None) -> RoleStore | None:
    roles = getattr(projects, "roles", None)
    return roles if isinstance(roles, RoleStore) else None


def _has_roles(namespace: Namespace) -> bool:
    """Whether a namespace can hold rows: the root or exactly one first-class project."""
    return not namespace.path or len(namespace.project_ids) == 1


def _role_project_id(namespace: Namespace) -> str | None:
    return next(iter(namespace.project_ids)) if namespace.path else None


def _namespace_role(
    roles: RoleStore | None, namespace: Namespace, name: str, user_id: str | None
) -> Role | None:
    if roles is None or not _has_roles(namespace):
        return None
    return roles.find(name, user_id=user_id, project_id=_role_project_id(namespace))


def _is_live(session: Conversation) -> bool:
    return not session.archived and not is_session_closed(session.labels, session.title)


def _live_holder(conversations: ConversationStore, role: Role) -> Conversation | None:
    """The role's holder, unless it is closed, archived or deleted."""
    if role.holder_session_id is None:
        return None
    session = conversations.get_conversation(role.holder_session_id)
    return session if session is not None and _is_live(session) else None


def _namespace_text(path: str) -> str:
    return path or "/"


def _where(path: str) -> str:
    return f"project {path!r}" if path else "unfiled sessions"


def _mirror_labels(role: Role) -> dict[str, str]:
    labels = {ALIAS_LABEL_KEY: role.name, ROLE_ID_LABEL_KEY: role.id}
    if role.overlay_ref:
        labels[ROLE_OVERLAY_LABEL_KEY] = role.overlay_ref
    return labels


def _write_mirror(conversations: ConversationStore, session: Conversation, role: Role) -> None:
    conversations.set_labels(session.id, _mirror_labels(role))
    if not role.overlay_ref and ROLE_OVERLAY_LABEL_KEY in session.labels:
        conversations.delete_label(session.id, ROLE_OVERLAY_LABEL_KEY)
    for key in ROLE_RELEASED_LABEL_KEYS:
        if key in session.labels:
            conversations.delete_label(session.id, key)


def _clear_mirror(conversations: ConversationStore, session_id: str) -> None:
    for key in _MIRROR_LABEL_KEYS:
        conversations.delete_label(session_id, key)


def _role_path(projects: ProjectStore | None, role: Role) -> str:
    if role.project_id is None or projects is None:
        return ""
    return projects.get_path(role.project_id, user_id=role.user_id) or ""


def _ago(epoch: int | None, now: float) -> str | None:
    if epoch is None:
        return None
    seconds = max(0, int(now - epoch))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _holder_state_text(status: HolderStatus | None, now: float) -> str | None:
    """``runner online`` / ``runner offline, runner last seen 3h ago``; ``None`` when unknown."""
    if status is None:
        return None
    if status.runner_online:
        return "runner online"
    seen = _ago(status.runner_last_seen, now)
    active = _ago(status.last_activity_at, now)
    text = "runner offline" if status.runner_online is False else "runner state unknown"
    if seen:
        text += f", runner last seen {seen}"
    elif status.runner_online is False:
        # A deregistered runner keeps no last-seen time.
        text += ", runner last seen not recorded"
    if active:
        text += f", session last active {active}"
    return text


def _held_error(
    name: str,
    path: str,
    holder_id: str | None,
    *,
    holder: Conversation | None = None,
    status: HolderStatus | None = None,
) -> OmnigentError:
    """Name the holder, its runner state and both ways to free the role."""
    who = f"session {holder_id}"
    if holder is not None and holder.title:
        who += f" ({holder.title!r})"
    state = _holder_state_text(status, time.time())
    if state:
        who += f", {state}"
    return OmnigentError(
        f"Role {name!r} in {_where(path)} is held by {who}. To free it, revive that "
        f"session (message or reopen it so its runner restarts), or ask the owner to "
        f"force-release it: sys_role_release(role={name!r}, force=true).",
        code=ErrorCode.CONFLICT,
    )


def _held_error_for(
    conversations: ConversationStore,
    role_name: str,
    path: str,
    holder_id: str | None,
    holder_status: HolderStatusLookup | None,
) -> OmnigentError:
    holder = conversations.get_conversation(holder_id) if holder_id else None
    status = holder_status([holder_id]).get(holder_id) if holder_status and holder_id else None
    return _held_error(role_name, path, holder_id, holder=holder, status=status)


def _require_in_namespace(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    session: Conversation,
    namespace: Namespace,
    user_id: str | None,
) -> None:
    """File an unfiled session into ``namespace``; refuse one filed elsewhere."""
    own = namespace_for_session(session, projects, user_id)
    if own.path == namespace.path:
        return
    if own.path:
        target = _namespace_text(namespace.path)
        raise OmnigentError(
            f"Session {session.id} is filed in {own.path}; file it in {target} first "
            f"(PATCH /v1/sessions/{session.id} with that project's id)",
            code=ErrorCode.INVALID_INPUT,
        )
    conversations.set_conversation_project(session.id, _role_project_id(namespace))


def take_role(
    conversations: ConversationStore,
    projects: ProjectStore,
    roles: RoleStore,
    *,
    session: Conversation,
    namespace: Namespace,
    user_id: str | None,
    name: str,
    overlay_ref: str | None = None,
    actor: str = "owner",
    holder_status: HolderStatusLookup | None = None,
) -> Role:
    """Make ``session`` hold ``name`` in ``namespace``, creating the row if needed.

    Decided here because the holder's state can live on another database; the
    store's generation CAS makes the decision safe. The row is written first, so
    a failure before the labels land is repaired by the next resolve.

    :param conversations: Store holding the session labels.
    :param projects: Project store, for paths.
    :param roles: Role store.
    :param session: The live top-level session taking the role.
    :param namespace: The role's namespace.
    :param user_id: The owner.
    :param name: Validated role name, in any case.
    :param overlay_ref: Validated role-file name, or ``None`` to keep the row's.
    :param actor: ``owner`` or ``agent``, recorded on the event.
    :param holder_status: Runner state lookup that a ``CONFLICT`` names.
    :returns: The role as written.
    :raises OmnigentError: ``INVALID_INPUT`` for a child, a closed or archived
        session or one filed elsewhere; ``CONFLICT`` when a live session holds
        the name, the session holds another role, or the race was lost.
    """
    if session.parent_conversation_id is not None:
        raise OmnigentError(
            "Only top-level sessions can have an alias", code=ErrorCode.INVALID_INPUT
        )
    if not _is_live(session):
        raise OmnigentError(
            f"Session {session.id} is closed or archived", code=ErrorCode.INVALID_INPUT
        )
    _require_in_namespace(conversations, projects, session, namespace, user_id)
    path = namespace.path
    role = _namespace_role(roles, namespace, name, user_id)
    held = roles.held_by([session.id])
    if held and (role is None or held[0].id != role.id):
        raise OmnigentError(
            f"Session {session.id} already holds role {held[0].name!r}; release it first",
            code=ErrorCode.CONFLICT,
        )
    if role is None:
        role = _create_role_row(
            conversations, roles, session, namespace, user_id, name, overlay_ref, actor
        )
    if role.holder_session_id == session.id:
        _write_mirror(conversations, session, role)
        return role
    stale_holder = role.holder_session_id
    if stale_holder is not None and _live_holder(conversations, role) is not None:
        raise _held_error_for(conversations, role.name, path, stale_holder, holder_status)
    taken = roles.take(
        role,
        session.id,
        kind="take" if stale_holder is None else "stale_repair",
        actor=actor,
        reason=None if stale_holder is None else "holder_not_live",
        role_path=path,
        overlay_ref=overlay_ref if overlay_ref is not None else role.overlay_ref,
    )
    if taken is None:
        current = roles.get(role.id, user_id=user_id)
        raise _held_error_for(
            conversations,
            role.name,
            path,
            current.holder_session_id if current else None,
            holder_status,
        )
    _write_mirror(conversations, session, taken)
    if stale_holder is not None:
        _clear_mirror(conversations, stale_holder)
    _bring_role_mail(conversations, projects, {stale_holder, taken.previous_session_id})
    return taken


def _bring_role_mail(
    conversations: ConversationStore, projects: ProjectStore, sources: set[str | None]
) -> None:
    """Move messages queued for the role on earlier holders to its new holder.

    Best effort: the inbox worker's sweep repairs any move this misses.
    """
    from omnigent.server.role_mail import relocate_role_mail
    from omnigent.server.startup_inputs import wake_startup_worker

    for source in {source for source in sources if source is not None}:
        try:
            for batch in relocate_role_mail(conversations, projects, source):
                if batch.host_id is not None:
                    wake_startup_worker(batch.host_id)
        except Exception:
            _logger.exception("Unable to move queued role messages from session %s", source)


def _create_role_row(
    conversations: ConversationStore,
    roles: RoleStore,
    session: Conversation,
    namespace: Namespace,
    user_id: str | None,
    name: str,
    overlay_ref: str | None,
    actor: str,
) -> Role:
    """Create a name's row, vacant or adopting ``session`` as its label holder.

    The label scan only reads: another session's label blocks the take rather
    than being adopted from under it.
    """
    holders = find_aliased_sessions(
        conversations, user_id=user_id, project=namespace.path, alias=name, namespace=namespace
    )
    if len(holders) > 1:
        raise ambiguous_alias_error(name, namespace.path, holders)
    if holders and holders[0].id != session.id:
        raise _held_error(name, namespace.path, holders[0].id)
    try:
        return roles.create(
            name,
            user_id=user_id,
            project_id=_role_project_id(namespace),
            overlay_ref=overlay_ref,
            holder_session_id=session.id if holders else None,
            actor=actor,
            reason="label" if holders else None,
            role_path=namespace.path,
        )
    except OmnigentError as exc:
        if exc.code != ErrorCode.ALREADY_EXISTS:
            raise
        raced = _namespace_role(roles, namespace, name, user_id)
        if raced is None:
            raise
        return raced


def set_session_alias(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    session: Conversation,
    user_id: str | None,
    alias: object,
    holder_status: HolderStatusLookup | None = None,
) -> str:
    """Take or create a role in the session's own namespace; returns the role name."""
    value = validate_alias(alias)
    if session.parent_conversation_id is not None:
        raise OmnigentError(
            "Only top-level sessions can have an alias", code=ErrorCode.INVALID_INPUT
        )
    namespace = namespace_for_session(session, projects, user_id)
    roles = _role_store(projects)
    if roles is None or projects is None or not _has_roles(namespace):
        _require_alias_free(conversations, user_id, namespace, value, holder_id=session.id)
        conversations.set_labels(session.id, {ALIAS_LABEL_KEY: value})
        return value
    return take_role(
        conversations,
        projects,
        roles,
        session=session,
        namespace=namespace,
        user_id=user_id,
        name=value,
        holder_status=holder_status,
    ).name


def require_role_takeable(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    session: Conversation,
    user_id: str | None,
    alias: object,
    holder_status: HolderStatusLookup | None = None,
) -> None:
    """Raise the take's conflict early, before an owner is asked to approve it.

    Read-only; :func:`take_role` still decides under the store's CAS.
    """
    value = validate_alias(alias)
    if session.parent_conversation_id is not None:
        raise OmnigentError(
            "Only top-level sessions can have an alias", code=ErrorCode.INVALID_INPUT
        )
    if not _is_live(session):
        raise OmnigentError(
            f"Session {session.id} is closed or archived", code=ErrorCode.INVALID_INPUT
        )
    namespace = namespace_for_session(session, projects, user_id)
    roles = _role_store(projects)
    if roles is None or projects is None or not _has_roles(namespace):
        _require_alias_free(conversations, user_id, namespace, value, holder_id=session.id)
        return
    role = _namespace_role(roles, namespace, value, user_id)
    held = roles.held_by([session.id])
    if held and (role is None or held[0].id != role.id):
        raise OmnigentError(
            f"Session {session.id} already holds role {held[0].name!r}; release it first",
            code=ErrorCode.CONFLICT,
        )
    if role is None:
        holders = find_aliased_sessions(
            conversations,
            user_id=user_id,
            project=namespace.path,
            alias=value,
            namespace=namespace,
        )
        if len(holders) > 1:
            raise ambiguous_alias_error(value, namespace.path, holders)
        if holders and holders[0].id != session.id:
            raise _held_error(value, namespace.path, holders[0].id)
        return
    holder_id = role.holder_session_id
    if holder_id not in (None, session.id) and _live_holder(conversations, role) is not None:
        raise _held_error_for(conversations, role.name, namespace.path, holder_id, holder_status)


def clear_session_alias(
    conversations: ConversationStore, projects: ProjectStore | None, session_id: str
) -> None:
    """Release the session's role, or drop a label alias that has no row.

    Unlike the lifecycle hooks this raises on failure, so the caller sees an
    error while the row still holds the role and can retry.
    """
    released = release_session_role(
        conversations, projects, session_id, actor="owner", reason="alias_cleared"
    )
    if released is None:
        _clear_mirror(conversations, session_id)


def check_alias_for_new_session(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    alias: object,
    project_id: str | None,
    user_id: str | None,
    inherit_from: Conversation | None = None,
) -> str:
    """Validate an alias requested at creation, before anything is created.

    Fails fast only: the claim after creation is what decides a race.
    ``inherit_from`` overrides ``project_id`` when the new session will join that
    session's project.
    """
    value = validate_alias(alias)
    if inherit_from is not None:
        namespace = namespace_for_session(inherit_from, projects, user_id)
    else:
        namespace = (
            namespace_for_project(project_id, projects, user_id)
            if project_id and projects
            else None
        ) or Namespace("", "")
    role = _namespace_role(_role_store(projects), namespace, value, user_id)
    if role is None:
        _require_alias_free(conversations, user_id, namespace, value, holder_id=None)
    elif _live_holder(conversations, role) is not None:
        raise _alias_taken_error(value, namespace.path)
    return value


def claim_alias_for_new_session(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    session_id: str,
    user_id: str | None,
    alias: object,
    overlay_ref: object = None,
    actor: str = "owner",
) -> None:
    """Claim a role for a session just created and filed: create, then claim.

    There is no pre-claim, so a holder that does not exist yet is never mistaken
    for a stale one. The caller closes the session when this raises.

    :raises OmnigentError: ``ALREADY_EXISTS`` when the claim was lost.
    """
    value = validate_alias(alias)
    overlay = validate_overlay_ref(overlay_ref)
    session = conversations.get_conversation(session_id)
    if session is None:
        raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
    namespace = namespace_for_session(session, projects, user_id)
    roles = _role_store(projects)
    if roles is None or projects is None or not _has_roles(namespace):
        _require_alias_free(conversations, user_id, namespace, value, holder_id=session.id)
        labels = {ALIAS_LABEL_KEY: value}
        if overlay is not None and overlay.casefold() != value.casefold():
            labels[ROLE_OVERLAY_LABEL_KEY] = overlay
        conversations.set_labels(session.id, labels)
        return
    try:
        take_role(
            conversations,
            projects,
            roles,
            session=session,
            namespace=namespace,
            user_id=user_id,
            name=value,
            overlay_ref=overlay,
            actor=actor,
        )
    except OmnigentError as exc:
        if exc.code != ErrorCode.CONFLICT:
            raise
        raise _alias_taken_error(value, namespace.path) from exc


def ambiguous_alias_error(
    alias: str, project: str, candidates: list[Conversation]
) -> OmnigentError:
    """Conflict error naming every session that shares one alias in a project."""
    where = f"project {project!r}" if project else "unfiled sessions"
    ids = ", ".join(session.id for session in candidates)
    return OmnigentError(
        f"Alias {alias!r} is ambiguous in {where}: matches sessions {ids}; address one by id",
        code=ErrorCode.CONFLICT,
    )


def _alias_taken_error(alias: str, path: str) -> OmnigentError:
    return OmnigentError(
        f"Alias {alias!r} is already used in {_where(path)}", code=ErrorCode.ALREADY_EXISTS
    )


def _require_alias_free(
    conversations: ConversationStore,
    user_id: str | None,
    namespace: Namespace,
    alias: str,
    *,
    holder_id: str | None,
) -> None:
    holders = find_aliased_sessions(
        conversations, user_id=user_id, project=namespace.path, alias=alias, namespace=namespace
    )
    if len(holders) > 1:
        raise ambiguous_alias_error(alias, namespace.path, holders)
    if any(holder.id != holder_id for holder in holders):
        raise _alias_taken_error(alias, namespace.path)


def release_session_role(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    session_id: str,
    *,
    actor: str = "system",
    reason: str | None = None,
    role_path: str | None = None,
) -> Role | None:
    """Release what ``session_id`` holds; does nothing when it holds no row.

    Labels go first, so a crash leaves the row held and reconcile restores the
    label: an error here means the role is still held and the call must be
    retried, never that it was silently dropped.

    :param conversations: Store holding the session labels.
    :param projects: Project store, for paths.
    :param session_id: The holder.
    :param actor: Recorded on the event.
    :param reason: Recorded on the event, e.g. ``"archive"``.
    :param role_path: Path to snapshot when the project is already gone.
    :returns: The vacated role, or ``None`` when the session held none.
    :raises OmnigentError: ``CONFLICT`` when the row keeps moving under the write.
    """
    roles = _role_store(projects)
    if roles is None:
        return None
    for _ in range(_RELEASE_CAS_ATTEMPTS):
        held = roles.held_by([session_id])
        if not held:
            return None
        role = held[0]
        _clear_mirror(conversations, session_id)
        released = roles.release(
            role,
            actor=actor,
            reason=reason,
            role_path=_role_path(projects, role) if role_path is None else role_path,
        )
        if released is not None:
            return released
    raise OmnigentError(
        f"Could not release the role of session {session_id}; try again",
        code=ErrorCode.CONFLICT,
    )


def release_role_after_lifecycle(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    session_id: str,
    *,
    reason: str,
    role_path: str | None = None,
    sleep_seconds: float | None = None,
) -> None:
    """Release after a committed close, archive, unarchive, delete or move.

    Retries, then logs instead of raising: the lifecycle change already landed,
    and the next take treats a missed release as a stale holder.
    """
    for attempt in range(1, _RELEASE_HOOK_ATTEMPTS + 1):
        try:
            release_session_role(
                conversations, projects, session_id, reason=reason, role_path=role_path
            )
            return
        except Exception:
            if attempt == _RELEASE_HOOK_ATTEMPTS:
                _logger.exception("Releasing the role of session %s failed", session_id)
                return
            time.sleep(
                (_RELEASE_HOOK_BACKOFF_S if sleep_seconds is None else sleep_seconds) * attempt
            )


def release_project_roles(
    conversations: ConversationStore,
    projects: ProjectStore,
    project_id: str,
    *,
    session_ids: Iterable[str],
    user_id: str | None,
    role_path: str,
) -> None:
    """Release a deleted project's roles and those of its swept members, post-commit.

    A project sweep archives set-based in the store, past the per-session hooks.

    :param conversations: Store holding the session labels.
    :param projects: Project store.
    :param project_id: The project that was deleted.
    :param session_ids: Sessions swept by the delete.
    :param user_id: The owner.
    :param role_path: The project's path, read before the delete.
    """
    roles = _role_store(projects)
    if roles is None:
        return
    in_project = roles.list_namespace(user_id=user_id, project_id=project_id)
    holders = {role.holder_session_id for role in in_project} | {
        role.holder_session_id for role in roles.held_by(session_ids)
    }
    for holder in sorted(holder for holder in holders if holder is not None):
        release_role_after_lifecycle(
            conversations, projects, holder, reason="project_delete", role_path=role_path
        )


@dataclass(frozen=True)
class ForceRelease:
    """A force-release decided before the owner's approval, applied after it.

    :param role: The row as read; the write is a CAS on its generation.
    :param namespace: The role's namespace.
    :param holder: The holder being removed, if its session still exists.
    :param target: The session that takes the role, or ``None`` to leave it vacant.
    """

    role: Role
    namespace: Namespace
    holder: Conversation | None
    target: Conversation | None

    @property
    def path(self) -> str:
        """The role's namespace path."""
        return self.namespace.path

    @property
    def handle(self) -> str:
        """The role's ``path/Role`` handle."""
        return role_handle(self.path, self.role.name)


def plan_force_release(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    namespace: Namespace,
    user_id: str | None,
    name: object,
    give_to: object = None,
    default_namespace: Namespace | None = None,
) -> ForceRelease:
    """Check a force-release of ``name`` and snapshot the row it will CAS on.

    :param conversations: Store holding the sessions.
    :param projects: Project store.
    :param namespace: The role's namespace.
    :param user_id: The owner.
    :param name: The role name.
    :param give_to: A session id or role address to hand the role to, or ``None``.
    :param default_namespace: Namespace a bare ``give_to`` role is relative to.
    :returns: The plan to apply after approval.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown role or target;
        ``INVALID_INPUT`` for a vacant role or an unfit target.
    """
    value = validate_alias(name)
    roles = _role_store(projects)
    role = _namespace_role(roles, namespace, value, user_id)
    if role is None:
        raise OmnigentError(
            f"No role {value!r} with a record in namespace {_namespace_text(namespace.path)!r}; "
            "use sys_role_list to see the roles here",
            code=ErrorCode.NOT_FOUND,
        )
    if role.holder_session_id is None:
        raise OmnigentError(
            f"Role {role.name!r} in {_where(namespace.path)} is already vacant; "
            f"take it with sys_role_take(role={role.name!r})",
            code=ErrorCode.INVALID_INPUT,
        )
    holder = conversations.get_conversation(role.holder_session_id)
    target = None
    if give_to is not None:
        target_id = resolve_session_address(
            conversations,
            projects,
            address=give_to,
            user_id=user_id,
            source=None,
            default_namespace=default_namespace or namespace,
        )
        target = conversations.get_conversation(target_id)
        _require_fit_target(roles, projects, role, target, target_id, namespace, user_id)
    return ForceRelease(role=role, namespace=namespace, holder=holder, target=target)


def plan_role_permanent(
    projects: ProjectStore | None,
    *,
    namespace: Namespace,
    user_id: str | None,
    name: object,
) -> Role:
    """Snapshot the role row a permanent-flag change will CAS on.

    :raises OmnigentError: ``NOT_FOUND`` for a name with no role record.
    """
    value = validate_alias(name)
    role = _namespace_role(_role_store(projects), namespace, value, user_id)
    if role is None:
        raise OmnigentError(
            f"No role {value!r} with a record in namespace {_namespace_text(namespace.path)!r}; "
            "use sys_role_list to see the roles here",
            code=ErrorCode.NOT_FOUND,
        )
    return role


def apply_role_permanent(projects: ProjectStore | None, role: Role, permanent: bool) -> Role:
    """Write an approved permanent flag as one CAS on the planned generation.

    :raises OmnigentError: ``CONFLICT`` when the role changed since the plan.
    """
    roles = _role_store(projects)
    if roles is None:
        raise OmnigentError("Roles are not available here", code=ErrorCode.INVALID_INPUT)
    written = roles.set_permanent(role, permanent)
    if written is None:
        raise OmnigentError(
            f"Role {role.name!r} changed while the owner was asked (generation "
            f"{role.generation}); nothing was changed. Check sys_role_list and ask again.",
            code=ErrorCode.CONFLICT,
        )
    return written


def _require_fit_target(
    roles: RoleStore | None,
    projects: ProjectStore | None,
    role: Role,
    target: Conversation | None,
    target_id: str,
    namespace: Namespace,
    user_id: str | None,
) -> None:
    """A target must be a live top-level session in the namespace holding no role."""
    if target is None:
        raise OmnigentError(f"Session {target_id} not found", code=ErrorCode.NOT_FOUND)
    if target.id == role.holder_session_id:
        raise OmnigentError(
            f"Session {target.id} already holds {role.name!r}", code=ErrorCode.INVALID_INPUT
        )
    if target.parent_conversation_id is not None or not _is_live(target):
        raise OmnigentError(
            f"Session {target.id} must be a live top-level session to take a role",
            code=ErrorCode.INVALID_INPUT,
        )
    if namespace_for_session(target, projects, user_id).path != namespace.path:
        raise OmnigentError(
            f"Session {target.id} is not in {_where(namespace.path)}",
            code=ErrorCode.INVALID_INPUT,
        )
    held = roles.held_by([target.id]) if roles is not None else []
    if held:
        raise OmnigentError(
            f"Session {target.id} already holds role {held[0].name!r}; release it first",
            code=ErrorCode.INVALID_INPUT,
        )


def apply_force_release(
    conversations: ConversationStore,
    projects: ProjectStore,
    plan: ForceRelease,
    *,
    requester_id: str | None,
    user_id: str | None,
) -> Role:
    """Apply an approved force-release as one CAS on the planned generation.

    The record's actor is ``owner`` and its reason ``forced:<requesting session id>``
    (empty for the owner's own call). The old holder loses its labels and gets a
    release notice; mail queued for the role follows it to the new holder.

    :raises OmnigentError: ``CONFLICT`` when the role changed since the plan;
        nothing is changed then.
    """
    roles = _role_store(projects)
    if roles is None:
        raise OmnigentError("Roles are not available here", code=ErrorCode.INVALID_INPUT)
    role = plan.role
    old_holder = role.holder_session_id
    reason = f"forced:{requester_id or ''}"
    target = None
    if plan.target is not None:
        target = conversations.get_conversation(plan.target.id)
        _require_fit_target(
            roles,
            projects,
            role,
            target,
            plan.target.id,
            plan.namespace,
            user_id,
        )
    if target is not None:
        written = roles.take(
            role,
            target.id,
            kind="takeover",
            actor="owner",
            reason=reason,
            role_path=plan.path,
            overlay_ref=role.overlay_ref,
        )
    else:
        # Labels first, as for any release; restored below if the CAS loses.
        if old_holder is not None:
            _clear_mirror(conversations, old_holder)
        try:
            written = roles.release(role, actor="owner", reason=reason, role_path=plan.path)
        except Exception:
            if plan.holder is not None:
                _write_mirror(conversations, plan.holder, role)
            raise
    if written is None:
        current = roles.get(role.id, user_id=user_id)
        if target is None and plan.holder is not None and current is not None:
            if current.holder_session_id == old_holder:
                _write_mirror(conversations, plan.holder, current)
        now_generation = current.generation if current is not None else "deleted"
        raise OmnigentError(
            f"Role {role.name!r} changed while the owner was asked (generation "
            f"{role.generation} is now {now_generation}); nothing was changed. "
            "Check sys_role_list and ask again.",
            code=ErrorCode.CONFLICT,
        )
    if target is not None:
        _write_mirror(conversations, target, written)
    if old_holder is not None:
        if target is not None:
            _clear_mirror(conversations, old_holder)
        _mark_released(conversations, old_holder, plan.handle, requester_id, target)
    if target is not None:
        _bring_role_mail(conversations, projects, {old_holder})
    return written


def _mark_released(
    conversations: ConversationStore,
    session_id: str,
    handle: str,
    requester_id: str | None,
    target: Conversation | None,
) -> None:
    """Leave the old holder its release notice; best effort, the row already moved."""
    try:
        conversations.set_labels(
            session_id,
            {
                ROLE_RELEASED_LABEL_KEY: handle,
                ROLE_RELEASED_AT_LABEL_KEY: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ROLE_RELEASED_BY_LABEL_KEY: requester_id or "",
                ROLE_RELEASED_TO_LABEL_KEY: target.id if target is not None else "",
            },
        )
    except Exception:
        _logger.exception("Unable to record the role release notice on session %s", session_id)


def _reconcile_labels(
    conversations: ConversationStore,
    role: Role,
    holder: Conversation | None,
    labelled: list[Conversation],
) -> None:
    """Make the mirror match the row; touches labels only, never the row.

    A failure leaves the drift for the next resolve rather than failing this one.
    """
    try:
        for session in labelled:
            if holder is None or session.id != holder.id:
                _clear_mirror(conversations, session.id)
        if holder is not None and any(
            holder.labels.get(key) != value for key, value in _mirror_labels(role).items()
        ):
            _write_mirror(conversations, holder, role)
    except Exception:
        _logger.exception("Repairing the labels of role %s failed", role.id)


def _unresolved_error(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    namespace: Namespace,
    role: str,
    user_id: str | None,
) -> OmnigentError:
    """Name the role's neighbours, so the caller can correct the address."""
    here = find_aliased_sessions(
        conversations, user_id=user_id, project=namespace.path, namespace=namespace
    )
    names = sorted({session.labels[ALIAS_LABEL_KEY] for session in here}, key=str.casefold)
    elsewhere = sorted(
        {
            session_project_name(session, projects, user_id) or "/"
            for session in find_aliased_sessions(
                conversations, user_id=user_id, project=None, alias=role
            )
        }
        - {_namespace_text(namespace.path)}
    )
    parts = [
        f"No role {role!r} in namespace {_namespace_text(namespace.path)!r}.",
        f"Roles here: {', '.join(names) or 'none'}.",
    ]
    if elsewhere:
        parts.append(f"Other namespaces with {role}: {', '.join(elsewhere)}.")
    parts.append(f"Use '<path>/{role}' or sys_role_list.")
    return OmnigentError(" ".join(parts), code=ErrorCode.NOT_FOUND)


def workspace_namespace(
    projects: ProjectStore | None, workspace: str, user_id: str | None
) -> Namespace:
    """The namespace of the project whose ``config.workspace`` covers ``workspace``.

    The deepest match wins, so a subdirectory of a project's checkout counts.

    :returns: That project's namespace, or the root when none matches.
    :raises OmnigentError: ``CONFLICT`` when two projects claim the same directory.
    """
    if projects is None or not workspace.strip():
        return Namespace("", "")
    target = PurePosixPath(workspace.strip())
    matches: list[tuple[int, str]] = []
    for project in projects.list(user_id=user_id):
        configured = project.config.get("workspace")
        if not isinstance(configured, str) or not configured.strip():
            continue
        root = PurePosixPath(configured.strip())
        if target == root or root in target.parents:
            matches.append((len(root.parts), project.id))
    if not matches:
        return Namespace("", "")
    deepest = max(depth for depth, _ in matches)
    ids = [project_id for depth, project_id in matches if depth == deepest]
    if len(ids) > 1:
        raise OmnigentError(
            f"Workspace {workspace!r} matches several projects: {', '.join(ids)}",
            code=ErrorCode.CONFLICT,
        )
    return namespace_for_project(ids[0], projects, user_id) or Namespace("", "")


def source_namespace(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    user_id: str | None,
    source: Conversation | None = None,
    view: str | None = None,
    workspace: str | None = None,
) -> Namespace:
    """The namespace a bare ``Role`` is relative to, for each kind of caller.

    :param conversations: Store used to resolve ``view``.
    :param projects: Project store.
    :param user_id: The owner.
    :param source: An agent's own session: its project.
    :param view: The project path a web picker has in view (``/`` is the root).
    :param workspace: The CLI's working directory: the project of that workspace.
    :returns: The first that applies, else the owner's unfiled root.
    """
    if source is not None:
        return namespace_for_session(source, projects, user_id)
    if view is not None:
        return resolve_namespace(conversations, projects, view.strip().strip("/"), user_id)
    if workspace is not None:
        return workspace_namespace(projects, workspace, user_id)
    return Namespace("", "")


def resolve_session_address(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    address: object,
    user_id: str | None,
    source: Conversation | None,
    default_namespace: Namespace | None = None,
) -> str:
    """Resolve a UUID, ``Role``, ``/Role`` or ``<path>/Role`` to a session id.

    The row decides and the labels are repaired to match it; a name with no row
    falls back to the label scan. Neither path ever creates a row.

    :param conversations: Store holding the session labels.
    :param projects: Project store.
    :param address: The address text.
    :param user_id: The owner.
    :param source: An agent's session, which a bare ``Role`` is relative to.
    :param default_namespace: Overrides ``source`` for a bare ``Role``.
    :returns: The holding session's id.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown or vacant role;
        ``CONFLICT`` when several sessions carry the label of a rowless name.
    """
    text = address.strip() if isinstance(address, str) else ""
    if not text:
        raise OmnigentError("Provide a session id or alias", code=ErrorCode.INVALID_INPUT)
    try:
        return uuid_to_bytes(text).hex()
    except ValueError:
        pass
    project_name, _, role = text.rpartition("/")
    role = validate_alias(role)
    if not project_name and "/" not in text:
        namespace = default_namespace or namespace_for_session(source, projects, user_id)
    else:
        namespace = resolve_namespace(conversations, projects, project_name, user_id)
    project_name = namespace.path
    matches = find_aliased_sessions(
        conversations,
        user_id=user_id,
        project=project_name,
        alias=role,
        namespace=namespace if projects is not None else None,
    )
    row = _namespace_role(_role_store(projects), namespace, role, user_id)
    if row is not None:
        holder = _live_holder(conversations, row)
        _reconcile_labels(conversations, row, holder, matches)
        if holder is None:
            last = row.holder_session_id or row.previous_session_id
            last_text = f" It was last held by session {last}." if last else ""
            raise OmnigentError(
                f"Role {row.name!r} in namespace {_namespace_text(project_name)!r} is vacant."
                f"{last_text} A session in that namespace can take it with "
                f"sys_role_take(role={row.name!r}); sys_role_list shows the roles here.",
                code=ErrorCode.NOT_FOUND,
            )
        return holder.id
    if not matches:
        raise _unresolved_error(conversations, projects, namespace, role, user_id)
    if len(matches) > 1:
        raise ambiguous_alias_error(role, project_name, matches)
    return matches[0].id


def alias_entries(
    sessions: Iterable[Conversation], projects: ProjectStore | None, user_id: str | None
) -> list[dict[str, Any]]:
    """Listing rows for aliased sessions, without labels the row no longer backs."""
    found = list(sessions)
    roles = _role_store(projects)
    held = (
        {role.holder_session_id: role for role in roles.held_by(s.id for s in found)}
        if roles is not None
        else {}
    )
    entries: list[dict[str, Any]] = []
    for session in found:
        role = held.get(session.id)
        mirrored = session.labels.get(ROLE_ID_LABEL_KEY)
        if mirrored is not None and (role is None or role.id != mirrored):
            continue
        path = session_project_name(session, projects, user_id)
        entries.append(
            {
                "session_id": session.id,
                "alias": session.labels.get(ALIAS_LABEL_KEY),
                "project": path,
                "path": path,
                "handle": session_handle(session, projects, user_id),
                "role_id": role.id if role is not None else None,
                "permanent": role.permanent if role is not None else False,
                "title": session.title,
                "host_id": session.host_id,
            }
        )
    return entries


def list_namespace_roles(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    namespace: Namespace,
    user_id: str | None,
    holder_status: HolderStatusLookup | None = None,
) -> list[dict[str, Any]]:
    """Roles of one namespace, held and vacant, plus aliases that have no row yet.

    Held rows carry the holder's ``runner_online``, ``runner_last_seen`` and
    ``last_activity_at`` from ``holder_status``; vacant rows carry ``None``.
    """
    path = namespace.path
    roles = _role_store(projects)
    rows = (
        roles.list_namespace(user_id=user_id, project_id=_role_project_id(namespace))
        if roles is not None and _has_roles(namespace)
        else []
    )
    entries: list[dict[str, Any]] = []
    for role in rows:
        holder = _live_holder(conversations, role)
        entries.append(
            {
                "role_id": role.id,
                "name": role.name,
                "handle": role_handle(path, role.name),
                "path": path,
                "permanent": role.permanent,
                "state": "held" if holder is not None else "vacant",
                "holder_session_id": holder.id if holder is not None else None,
            }
        )
    named = {role.name_key for role in rows}
    for session in find_aliased_sessions(
        conversations, user_id=user_id, project=path, namespace=namespace
    ):
        alias = session.labels[ALIAS_LABEL_KEY]
        if alias.casefold() not in named:
            entries.append(
                {
                    "role_id": None,
                    "name": alias,
                    "handle": role_handle(path, alias),
                    "path": path,
                    "permanent": False,
                    "state": "held",
                    "holder_session_id": session.id,
                }
            )
    held_ids = sorted({e["holder_session_id"] for e in entries if e["holder_session_id"]})
    statuses = holder_status(held_ids) if holder_status is not None and held_ids else {}
    for entry in entries:
        status = statuses.get(entry["holder_session_id"]) or HolderStatus()
        entry["runner_online"] = status.runner_online
        entry["runner_last_seen"] = status.runner_last_seen
        entry["last_activity_at"] = status.last_activity_at
    return sorted(entries, key=lambda entry: entry["name"].casefold())


def messaging_handle(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str:
    """``path/Role`` for an aliased session, else ``path/`` (``/`` when unfiled)."""
    return session_handle(session, projects, user_id) or role_handle(
        session_project_name(session, projects, user_id), ""
    )


def messaging_route_resolver(
    conversations: ConversationStore, projects: ProjectStore | None
) -> "HandleResolver":
    """Build the role resolver the messaging-rules policy uses to name both ends.

    The resolver returns ``None`` when either end does not resolve; the message
    route then reports the bad address itself.
    """
    from omnigent.policies.builtins.messaging import MessageRoute

    def resolve(sender_id: str, address: str, user_id: str | None) -> MessageRoute | None:
        sender = conversations.get_conversation(sender_id)
        if sender is None:
            return None
        try:
            target_id = resolve_session_address(
                conversations, projects, address=address, user_id=user_id, source=sender
            )
        except OmnigentError:
            return None
        target = conversations.get_conversation(target_id)
        if target is None:
            return None
        return MessageRoute(
            messaging_handle(sender, projects, user_id),
            messaging_handle(target, projects, user_id),
        )

    return resolve

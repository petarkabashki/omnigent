"""Owner-set role aliases that address top-level sessions as ``Role`` or ``path/Role``."""

from dataclasses import dataclass

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.session_alias_client import ALIAS_PATTERN
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.project_store import ProjectStore, lookup_project_path
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import uuid_to_bytes

_PAGE_SIZE = 200
_MAX_PAGES = 50


def validate_alias(alias: object) -> str:
    """Return a trimmed alias or raise ``INVALID_INPUT``."""
    value = alias.strip() if isinstance(alias, str) else ""
    if not ALIAS_PATTERN.match(value):
        raise OmnigentError(
            "Alias must start with a letter and use 1–32 letters, digits, '-' or '_'",
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


def session_handle(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str | None:
    """``<path>/Role`` (or bare ``Role`` when unfiled) for an aliased session, else ``None``."""
    alias = session.labels.get(ALIAS_LABEL_KEY)
    if not alias:
        return None
    project = session_project_name(session, projects, user_id)
    return f"{project}/{alias}" if project else alias


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


def set_session_alias(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    session: Conversation,
    user_id: str | None,
    alias: object,
) -> str:
    """Give a top-level session a role alias unique within its owner's project."""
    value = validate_alias(alias)
    if session.parent_conversation_id is not None:
        raise OmnigentError(
            "Only top-level sessions can have an alias", code=ErrorCode.INVALID_INPUT
        )
    namespace = namespace_for_session(session, projects, user_id)
    _require_alias_free(conversations, user_id, namespace, value, holder_id=session.id)
    conversations.set_labels(session.id, {ALIAS_LABEL_KEY: value})
    return value


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
    _require_alias_free(conversations, user_id, namespace, value, holder_id=None)
    return value


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
    if any(holder.id != holder_id for holder in holders):
        where = f"project {namespace.path!r}" if namespace.path else "unfiled sessions"
        raise OmnigentError(
            f"Alias {alias!r} is already used in {where}", code=ErrorCode.ALREADY_EXISTS
        )


def resolve_session_address(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    address: object,
    user_id: str | None,
    source: Conversation | None,
) -> str:
    """Resolve a UUID, ``Role`` (the source's namespace) or ``<path>/Role`` to a session id."""
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
        namespace = namespace_for_session(source, projects, user_id)
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
    if not matches:
        where = f"project {project_name!r}" if project_name else "unfiled sessions"
        raise OmnigentError(f"No live session named {role!r} in {where}", code=ErrorCode.NOT_FOUND)
    if len(matches) > 1:
        raise ambiguous_alias_error(role, project_name, matches)
    return matches[0].id

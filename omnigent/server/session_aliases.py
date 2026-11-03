"""Owner-set role aliases that address top-level sessions as ``Role`` or ``project/Role``."""

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.session_alias_client import ALIAS_PATTERN
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.project_store import ProjectStore
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


def session_project_name(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str:
    """Name of the session's first-class project, else its legacy project label, else ``""``."""
    if session.project_id and projects is not None:
        project = projects.get(session.project_id, user_id=user_id)
        if project is not None:
            return project.name
    return session.labels.get(PROJECT_LABEL_KEY, "")


def session_handle(
    session: Conversation, projects: ProjectStore | None, user_id: str | None
) -> str | None:
    """``project/Role`` (or bare ``Role`` when unfiled) for an aliased session, else ``None``."""
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
) -> list[Conversation]:
    """Live top-level aliased sessions the user owns.

    ``project`` is a project name (``""`` = unfiled, ``None`` = any project);
    ``alias`` matches case-insensitively (``None`` = any alias).
    """
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
            if wanted is None or value.casefold() == wanted:
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
    project = session_project_name(session, projects, user_id)
    _require_alias_free(conversations, user_id, project, value, holder_id=session.id)
    conversations.set_labels(session.id, {ALIAS_LABEL_KEY: value})
    return value


def check_alias_for_new_session(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    alias: object,
    project_id: str | None,
    user_id: str | None,
) -> str:
    """Validate an alias requested at creation, before anything is created."""
    value = validate_alias(alias)
    project = ""
    if project_id and projects is not None:
        found = projects.get(project_id, user_id=user_id)
        project = found.name if found is not None else ""
    _require_alias_free(conversations, user_id, project, value, holder_id=None)
    return value


def _require_alias_free(
    conversations: ConversationStore,
    user_id: str | None,
    project: str,
    alias: str,
    *,
    holder_id: str | None,
) -> None:
    holders = find_aliased_sessions(conversations, user_id=user_id, project=project, alias=alias)
    if any(holder.id != holder_id for holder in holders):
        where = f"project {project!r}" if project else "unfiled sessions"
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
    """Resolve a UUID, ``Role`` (the source's project) or ``project/Role`` to a session id."""
    text = address.strip() if isinstance(address, str) else ""
    if not text:
        raise OmnigentError("Provide a session id or alias", code=ErrorCode.INVALID_INPUT)
    try:
        return uuid_to_bytes(text).hex()
    except ValueError:
        pass
    project_name, _, role = text.rpartition("/")
    if not project_name and "/" not in text:
        project_name = session_project_name(source, projects, user_id) if source else ""
    role = validate_alias(role)
    matches = find_aliased_sessions(
        conversations, user_id=user_id, project=project_name.strip(), alias=role
    )
    if not matches:
        where = f"project {project_name!r}" if project_name else "unfiled sessions"
        raise OmnigentError(f"No live session named {role!r} in {where}", code=ErrorCode.NOT_FOUND)
    return matches[0].id

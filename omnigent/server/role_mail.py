"""Queued messages sent to a role follow the role to its current holder.

A pending input that records ``startup_options.role_id`` is delivered only on
the session that holds that role. On any other session it is *parked*, and
:func:`relocate_role_mail` moves it once the role has a live holder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    ItemData,
    MessageData,
    SlashCommandData,
    StartupInputOptions,
)
from omnigent.runner.session_requests import normalize_session_id
from omnigent.server.startup_inputs import (
    MOVED_FROM_CLOSED_NOTICE,
    add_delivery_markers,
    sent_before_held_marker,
)
from omnigent.stores.conversation_store import ROLE_ID_LABEL_KEY, ConversationStore
from omnigent.stores.project_store import ProjectStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MovedMail:
    """Inputs moved from one session to the role's current holder."""

    from_session_id: str
    to_session_id: str
    host_id: str | None
    items: list[ConversationItem]


def input_role_id(item: ConversationItem) -> str | None:
    """The role a pending input was addressed to, or ``None`` for a session-id send."""
    data = item.data
    options = data.startup_options if isinstance(data, (MessageData, SlashCommandData)) else None
    return options.role_id if options is not None else None


def addressed_to_role(address: object) -> bool:
    """Whether a peer message was addressed by role handle rather than by session id."""
    if not isinstance(address, str):
        return False
    try:
        normalize_session_id(address)
    except ValueError:
        return True
    return False


def is_parked(item: ConversationItem, conv: Conversation) -> bool:
    """Whether queued ``item`` waits for a role that ``conv`` no longer holds.

    A claimed row stays with the session that claimed it until its receipt arrives.
    """
    role_id = input_role_id(item)
    return (
        item.status == "in_progress"
        and role_id is not None
        and conv.labels.get(ROLE_ID_LABEL_KEY) != role_id
    )


def _moved_data(item: ConversationItem, role_handle: str) -> ItemData:
    data = item.data
    if not isinstance(data, (MessageData, SlashCommandData)):
        return data
    options = data.startup_options or StartupInputOptions()
    update: dict[str, object] = {"startup_options": options.model_copy(update={"moved": True})}
    if isinstance(data, MessageData):
        markers = [sent_before_held_marker(role_handle)]
        if item.status == "incomplete":
            markers.append(MOVED_FROM_CLOSED_NOTICE)
        update["content"] = add_delivery_markers(data.content, markers)
    return data.model_copy(update=update)


def relocate_role_mail(
    conversations: ConversationStore, projects: ProjectStore | None, session_id: str
) -> list[MovedMail]:
    """Move ``session_id``'s parked inputs to their roles' live holders.

    Only queued rows move. A claimed row moves too when this session is closed
    or archived, since it can no longer run it; it is marked as a possible repeat.
    Rows whose role is vacant stay parked here.
    """
    from omnigent.server.session_aliases import _is_live, _role_store, session_handle

    roles = _role_store(projects)
    conv = conversations.get_conversation(session_id)
    if roles is None or conv is None:
        return []
    live = _is_live(conv)
    groups: dict[tuple[str, str | None], list[ConversationItem]] = {}
    for item in conversations.list_pending_inputs(session_id):
        role_id = input_role_id(item)
        movable = is_parked(item, conv) or (item.status == "incomplete" and not live)
        if role_id is not None and movable:
            groups.setdefault((role_id, item.created_by), []).append(item)
    moved: list[MovedMail] = []
    for (role_id, owner), items in groups.items():
        role = roles.get(role_id, user_id=owner)
        holder_id = role.holder_session_id if role is not None else None
        if holder_id is None or holder_id == session_id:
            continue
        holder = conversations.get_conversation(holder_id)
        if holder is None or not _is_live(holder):
            continue
        handle = session_handle(holder, projects, owner) or role_id
        written = conversations.move_pending_inputs(
            session_id,
            holder_id,
            [item.id for item in items],
            statuses=("in_progress", "incomplete") if not live else ("in_progress",),
            transform=lambda item, handle=handle: _moved_data(item, handle),
        )
        if written:
            _logger.info(
                "Moved %d message(s) for role %s from session %s to %s",
                len(written),
                role_id,
                session_id,
                holder_id,
            )
            moved.append(MovedMail(session_id, holder_id, holder.host_id, written))
    return moved

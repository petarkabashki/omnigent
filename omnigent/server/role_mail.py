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


def _options(item: ConversationItem) -> StartupInputOptions | None:
    data = item.data
    return data.startup_options if isinstance(data, (MessageData, SlashCommandData)) else None


def waiting_for_holder(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    *,
    sender_id: str | None = None,
    session_id: str | None = None,
) -> list[tuple[str, ConversationItem]]:
    """Parked role mail whose role has no live holder, as ``(parked_on, item)``.

    Narrowed to one sender's messages, or to the rows parked on one session.
    """
    from omnigent.server.session_aliases import _is_live, _role_store

    roles = _role_store(projects)
    if roles is None:
        return []
    source_ids = (
        [session_id]
        if session_id is not None
        else conversations.list_pending_input_conversation_ids()
    )
    holder_live: dict[tuple[str, str | None], bool] = {}
    found: list[tuple[str, ConversationItem]] = []
    for source_id in source_ids:
        conv = conversations.get_conversation(source_id)
        if conv is None:
            continue
        for item in conversations.list_pending_inputs(source_id):
            options = _options(item)
            if not is_parked(item, conv) or options is None:
                continue
            if sender_id is not None and options.sender_id != sender_id:
                continue
            key = (options.role_id or "", item.created_by)
            if key not in holder_live:
                role = roles.get(key[0], user_id=item.created_by)
                holder = (
                    conversations.get_conversation(role.holder_session_id)
                    if role is not None and role.holder_session_id is not None
                    else None
                )
                holder_live[key] = holder is not None and _is_live(holder)
            if not holder_live[key]:
                found.append((source_id, item))
    return found


def _header_message_id(item: ConversationItem) -> str | None:
    from omnigent.server.role_threads import message_id_of_header

    data = item.data
    text = data.content[0].get("text") if isinstance(data, MessageData) and data.content else None
    return message_id_of_header(text.partition("\n")[0]) if isinstance(text, str) else None


def rescue_role_mail(
    conversations: ConversationStore, projects: ProjectStore | None, session_ids: list[str]
) -> set[str]:
    """Before ``session_ids`` are deleted, move their role mail to a live holder or drop it.

    A holder inside ``session_ids`` counts as vacant. A dropped message is counted on the
    sender's thread row and logged by message id. Returns the hosts of the holders that got mail.
    """
    from omnigent.server.role_threads import record_dropped
    from omnigent.server.session_aliases import _is_live, _role_store, session_handle

    roles = _role_store(projects)
    if roles is None:
        return set()
    doomed = set(session_ids)
    woken: set[str] = set()
    for session_id in session_ids:
        groups: dict[tuple[str, str | None], list[ConversationItem]] = {}
        for item in conversations.list_pending_inputs(session_id):
            role_id = input_role_id(item)
            if role_id is not None and item.status in ("in_progress", "incomplete"):
                groups.setdefault((role_id, item.created_by), []).append(item)
        for (role_id, owner), items in groups.items():
            role = roles.get(role_id, user_id=owner)
            holder_id = role.holder_session_id if role is not None else None
            holder = (
                conversations.get_conversation(holder_id)
                if holder_id is not None and holder_id not in doomed
                else None
            )
            if holder is not None and _is_live(holder):
                handle = session_handle(holder, projects, owner) or role_id
                moved = conversations.move_pending_inputs(
                    session_id,
                    holder.id,
                    [item.id for item in items],
                    statuses=("in_progress", "incomplete"),
                    transform=lambda item, handle=handle: _moved_data(item, handle),
                )
                if moved and holder.host_id is not None:
                    woken.add(holder.host_id)
                moved_ids = {item.id for item in moved}
                items = [item for item in items if item.id not in moved_ids]
            if not items:
                continue
            _logger.warning(
                "Dropped %d message(s) for role %s with session %s, which had no other live "
                "holder: %s",
                len(items),
                role_id,
                session_id,
                ", ".join(_header_message_id(item) or item.id for item in items),
            )
            for item in items:
                options = _options(item)
                if options is not None and options.sender_id and options.sender_thread:
                    record_dropped(conversations, options.sender_id, options.sender_thread)
    return woken


def waiting_by_sender(
    conversations: ConversationStore, projects: ProjectStore | None
) -> dict[str, dict[str, tuple[int, int]]]:
    """Messages waiting for a holder, as sender id -> thread key -> (count, oldest send time).

    Scans every session with pending rows; fine while rows are capped, not for a hot path.
    """
    found: dict[str, dict[str, tuple[int, int]]] = {}
    for _, item in waiting_for_holder(conversations, projects):
        options = _options(item)
        if options is None or not options.sender_id or not options.sender_thread:
            continue
        threads = found.setdefault(options.sender_id, {})
        count, oldest = threads.get(options.sender_thread, (0, item.created_at))
        threads[options.sender_thread] = (count + 1, min(oldest, item.created_at))
    return found


def waiting_counts(
    conversations: ConversationStore, projects: ProjectStore | None, sender_id: str
) -> dict[str, int]:
    """How many of ``sender_id``'s messages wait for a holder, per sender thread key."""
    waiting = waiting_by_sender(conversations, projects).get(sender_id, {})
    return {thread: count for thread, (count, _) in waiting.items()}


def waiting_thread_rows(
    conversations: ConversationStore,
    projects: ProjectStore | None,
    sender_id: str,
    counterpart: str,
) -> list[dict[str, object]]:
    """``sender_id``'s messages in one thread that still wait for a holder, as history rows.

    Scans every session with pending rows; fine while rows are capped, not for a hot path.
    """
    from omnigent.server.role_threads import _history_row, _item_text

    rows: list[dict[str, object]] = []
    for parked_on, item in waiting_for_holder(conversations, projects, sender_id=sender_id):
        options = _options(item)
        text = _item_text(item.data)
        if options is None or options.sender_thread != counterpart or text is None:
            continue
        row = _history_row(parked_on, item.id, item.created_at, text)
        rows.append({**row, "direction": "out", "waiting_holder": True})
    return rows

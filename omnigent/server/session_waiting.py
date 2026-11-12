"""What a session is waiting on, where, and since when.

Each source (open tool calls, background shells, sub-agents, approvals, queued
messages, role threads, async tasks, offline runner) is its own small function
returning :class:`WaitingItem` dicts; one failing source is logged and skipped.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, TypedDict

from omnigent.entities.conversation import Conversation, FunctionCallData, FunctionCallOutputData
from omnigent.runtime import pending_elicitations, pending_inputs
from omnigent.server import role_threads
from omnigent.server.async_task_view import open_async_tasks
from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

WaitingKind = Literal[
    "tool_call",
    "async_task",
    "background_shell",
    "subagent",
    "approval",
    "queued_message",
    "thread",
    "runner_offline",
]
_BUSY = ("running", "waiting")
_TOOL_CALL_SCAN = 60
_TITLE_LIMIT = 80

# Minutes after which a wait is flagged stale; runner_offline is always stale.
STALE_MINUTES: dict[str, int] = {
    "tool_call": 10,
    "async_task": 30,
    "background_shell": 30,
    "subagent": 30,
    "approval": 10,
    "queued_message": 5,
    "thread": 20,
    "runner_offline": 0,
}
_THREAD_WAITING_STATES = ("waiting_on_us", "waiting_on_owner", "blocked")
# Only these need the owner and count toward the Waiting badge.
_OWNER_KINDS = ("approval", "runner_offline")
_OWNER_THREAD_STATES = ("waiting_on_owner", "blocked")


class WaitingItem(TypedDict):
    """One thing a session is waiting on."""

    kind: str
    session_id: str
    title: str
    detail: str | None
    since: int | None
    where: str | None
    ref: str | None
    stale: bool
    needs_owner: bool
    actions: list[str]


def _short(text: object, limit: int = _TITLE_LIMIT) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _is_stale(kind: str, since: int | None, now: int) -> bool:
    limit = STALE_MINUTES[kind]
    if limit == 0:
        return True
    return since is not None and now - since > limit * 60


def make_item(
    kind: WaitingKind,
    session_id: str,
    title: str,
    *,
    now: int,
    since: int | None = None,
    detail: str | None = None,
    where: str | None = None,
    ref: str | None = None,
    actions: Iterable[str] = ("open",),
    needs_owner: bool | None = None,
) -> WaitingItem:
    """Build one item, computing ``stale`` from the kind's limit.

    ``needs_owner`` defaults from the kind; thread items pass it from their state.
    """
    return WaitingItem(
        kind=kind,
        session_id=session_id,
        title=title,
        detail=detail,
        since=since,
        where=where,
        ref=ref,
        stale=_is_stale(kind, since, now),
        needs_owner=kind in _OWNER_KINDS if needs_owner is None else needs_owner,
        actions=list(actions),
    )


def _args_preview(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return _short(arguments, 50)
    if isinstance(parsed, dict):
        for key in ("command", "cmd", "path", "file_path", "query", "url", "prompt", "message"):
            if isinstance(parsed.get(key), str):
                return _short(parsed[key], 50)
        return _short(json.dumps(parsed, separators=(",", ":")), 50)
    return _short(parsed, 50)


def open_tool_call_items(
    store: ConversationStore, conv: Conversation, *, now: int
) -> list[WaitingItem]:
    """Function calls in the last items with no matching output."""
    page = store.list_items(conv.id, limit=_TOOL_CALL_SCAN, order="desc")
    answered = {
        row.data.call_id for row in page.data if isinstance(row.data, FunctionCallOutputData)
    }
    items = []
    for row in reversed(page.data):
        data = row.data
        if not isinstance(data, FunctionCallData) or data.call_id in answered:
            continue
        if data.name == "sys_call_async":
            continue
        args = _args_preview(data.arguments)
        items.append(
            make_item(
                "tool_call",
                conv.id,
                f"{data.name}: {args}" if args else data.name,
                now=now,
                since=row.created_at,
                where=conv.host_id or "local",
                ref=row.id,
                actions=("open", "stop"),
            )
        )
    return items


def background_shell_items(
    conv: Conversation, shells: Iterable[Any], *, since: int | None, now: int
) -> list[WaitingItem]:
    """Running claude-native background shells; they carry no per-shell time."""
    items = []
    for shell in shells:
        get = shell.get if isinstance(shell, Mapping) else lambda k, s=shell: getattr(s, k, None)
        label = get("command") or get("description") or get("id") or "shell"
        items.append(
            make_item(
                "background_shell",
                conv.id,
                f"Shell: {_short(label, 60)}",
                now=now,
                since=since,
                detail=_short(get("description"), 80) if get("command") else None,
                where=conv.host_id or "local",
                ref=get("id"),
                actions=("open", "stop"),
            )
        )
    return items


def subagent_items(
    store: ConversationStore,
    conv: Conversation,
    *,
    status_of: Callable[[Conversation], str],
    now: int,
) -> list[WaitingItem]:
    """Child sessions whose live status is running or waiting."""
    page = store.list_conversations(limit=50, parent_conversation_id=conv.id, kind=None)
    items = []
    for child in page.data:
        status = status_of(child)
        if status not in _BUSY:
            continue
        name = child.sub_agent_name or child.title or child.id
        items.append(
            make_item(
                "subagent",
                conv.id,
                f"Sub-agent: {_short(name, 60)}",
                now=now,
                since=child.updated_at,
                detail=status,
                where=child.host_id or conv.host_id or "local",
                ref=child.id,
                actions=("open", "stop"),
            )
        )
    return items


def approval_items(conv: Conversation, *, since: int | None, now: int) -> list[WaitingItem]:
    """Outstanding elicitations; the index keeps no timestamp, so ``since`` is the status edge."""
    items = []
    for event in pending_elicitations.snapshot_for(conv.id):
        params = event.get("params")
        prompt = params.get("message") if isinstance(params, dict) else None
        items.append(
            make_item(
                "approval",
                conv.id,
                f"Approval: {_short(prompt or 'input requested', 70)}",
                now=now,
                since=since,
                ref=event.get("elicitation_id"),
            )
        )
    return items


def queued_message_items(conv: Conversation, *, now: int) -> list[WaitingItem]:
    """Web messages posted but not yet consumed by the agent."""
    return [
        make_item(
            "queued_message",
            conv.id,
            f"Queued: {_short(text or 'message', 70)}",
            now=now,
            since=enqueued,
            ref=pending_id,
        )
        for pending_id, enqueued, text in pending_inputs.queued_since(conv.id)
    ]


def thread_items(conv: Conversation, *, now: int) -> list[WaitingItem]:
    """Role threads the holder (or owner) must act on; others only once past the limit."""
    items = []
    for row in role_threads.thread_index(conv.labels):
        state = row["state"]
        since = row["updated_at"] or None
        if state not in _THREAD_WAITING_STATES:
            if state != "waiting_on_them" or not _is_stale("thread", since, now):
                continue
        digest = role_threads.thread_hash(row["counterpart"])
        nudges = conv.labels.get(f"thread.{digest}.nudges")
        parts = []
        if row["unread"]:
            parts.append(f"{row['unread']} unhandled")
        parts.append(state.replace("_", " "))
        if nudges and nudges != "0":
            parts.append(f"nudged {nudges}×")
        items.append(
            make_item(
                "thread",
                conv.id,
                _short(row["counterpart"], 60),
                now=now,
                since=since,
                detail=" · ".join(parts),
                ref=row["counterpart"],
                actions=("open", "nudge"),
                needs_owner=state in _OWNER_THREAD_STATES,
            )
        )
    return items


def async_task_items(
    store: ConversationStore, conv: Conversation, *, now: int
) -> list[WaitingItem]:
    """Dispatched ``sys_call_async`` tasks that are still open."""
    return [
        make_item(
            "async_task",
            conv.id,
            f"Async: {_short(task.get('tool') or task.get('task_id'), 60)}",
            now=now,
            since=task.get("since"),
            detail=task.get("detail"),
            where=conv.host_id or "local",
            ref=task.get("task_id"),
            actions=("open", "stop"),
        )
        for task in open_async_tasks(store, conv.id)
    ]


def runner_offline_items(
    conv: Conversation, *, status: str, runner_online: bool | None, since: int | None, now: int
) -> list[WaitingItem]:
    """A host-bound session marked busy whose runner is gone."""
    if conv.host_id is None or runner_online is not False or status not in _BUSY:
        return []
    return [
        make_item(
            "runner_offline",
            conv.id,
            "Runner offline",
            now=now,
            since=since,
            detail=f"{status} but the runner is not connected",
            where=conv.host_id,
            actions=("open",),
        )
    ]


def waiting_reason(
    status: str, *, shell_count: int = 0, approvals: int = 0, subagents: int = 0
) -> str | None:
    """Short phrase for why a session reads ``waiting`` (``None`` when it is not waiting)."""
    if status != "waiting":
        return None
    if approvals:
        return f"{approvals} approval{'s' if approvals != 1 else ''}"
    if subagents:
        return f"{subagents} sub-agent{'s' if subagents != 1 else ''}"
    if shell_count:
        return f"{shell_count} background shell{'s' if shell_count != 1 else ''}"
    return "background work"


def _safely(name: str, session_id: str, fn: Callable[[], list[WaitingItem]]) -> list[WaitingItem]:
    try:
        return fn()
    except Exception:
        _logger.warning("waiting source %s failed for %s", name, session_id, exc_info=True)
        return []


def build_session_waiting(
    store: ConversationStore,
    conv: Conversation,
    *,
    status: str,
    status_since: int | None,
    runner_online: bool | None,
    shells: Iterable[Any] = (),
    child_status_of: Callable[[Conversation], str] = lambda child: child.live_status or "idle",
    now: int | None = None,
) -> dict[str, Any]:
    """Assemble the ``SessionWaiting`` payload for one session."""
    stamp = int(time.time()) if now is None else now
    shell_list = list(shells)
    busy = status in _BUSY
    sources: list[tuple[str, Callable[[], list[WaitingItem]]]] = [
        ("tool_call", lambda: open_tool_call_items(store, conv, now=stamp) if busy else []),
        (
            "shells",
            lambda: background_shell_items(conv, shell_list, since=status_since, now=stamp),
        ),
        (
            "subagents",
            lambda: subagent_items(store, conv, status_of=child_status_of, now=stamp),
        ),
        ("approvals", lambda: approval_items(conv, since=status_since, now=stamp)),
        ("queued", lambda: queued_message_items(conv, now=stamp)),
        ("threads", lambda: thread_items(conv, now=stamp)),
        ("async", lambda: async_task_items(store, conv, now=stamp)),
        (
            "runner",
            lambda: runner_offline_items(
                conv, status=status, runner_online=runner_online, since=status_since, now=stamp
            ),
        ),
    ]
    items: list[WaitingItem] = []
    for name, source in sources:
        items.extend(_safely(name, conv.id, source))
    items.sort(key=lambda item: (item["since"] is None, item["since"] or 0))
    counts = {kind: sum(1 for item in items if item["kind"] == kind) for kind in STALE_MINUTES}
    return {
        "session_id": conv.id,
        "title": conv.title,
        "status": status if status in ("idle", "running", "waiting", "failed") else "idle",
        "status_since": status_since,
        "waiting_reason": waiting_reason(
            status,
            shell_count=counts["background_shell"],
            approvals=counts["approval"],
            subagents=counts["subagent"],
        ),
        "host_id": conv.host_id,
        "runner_online": runner_online,
        "items": items,
    }

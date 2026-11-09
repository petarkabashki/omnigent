"""Open ``sys_call_async`` tasks of a session, for the "waiting on" view."""

from __future__ import annotations

import json
import re
from typing import Any

from omnigent.entities.conversation import ConversationItem
from omnigent.stores.conversation_store import ConversationStore

# Recent items scanned; dispatches older than this window are not reported.
_SCAN_LIMIT = 400
_DETAIL_MAX = 120
_CANCEL_TOOLS = frozenset({"sys_cancel_async", "sys_cancel_task"})
_DISPATCH_TOOL = "sys_call_async"
_CLOSED = "completed|failed|cancelled"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
    return "\n".join(part for part in parts if isinstance(part, str))


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _detail(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("args")
    text = raw if isinstance(raw, str) else json.dumps(raw) if raw is not None else ""
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def open_async_tasks(store: ConversationStore, session_id: str) -> list[dict[str, Any]]:
    """
    Dispatched async tasks of ``session_id`` that have not completed or been cancelled.

    Derived from persisted items: a ``sys_call_async`` output carrying an
    ``in_progress`` handle opens a task; a later inbox line
    (``task <id> completed|failed|cancelled``) or a cancel call naming the
    handle closes it. Only the most recent items are scanned.

    :param store: Conversation store holding the session's items.
    :param session_id: Session to inspect.
    :returns: ``[{"task_id", "tool", "since", "detail"}]``, oldest first.
    """
    page = store.list_items(session_id, limit=_SCAN_LIMIT, order="desc")
    items: list[ConversationItem] = list(reversed(page.data))

    dispatch_args: dict[str, dict[str, Any]] = {}
    opened: dict[str, dict[str, Any]] = {}
    for item in items:
        data = item.data
        if item.type == "function_call":
            name = getattr(data, "name", "")
            arguments = _json_object(getattr(data, "arguments", ""))
            if name == _DISPATCH_TOOL:
                dispatch_args[getattr(data, "call_id", "")] = arguments
            elif name in _CANCEL_TOOLS:
                for key in ("handle_id", "task_id"):
                    opened.pop(str(arguments.get(key, "")), None)
        elif item.type == "function_call_output":
            call_id = getattr(data, "call_id", "")
            output = getattr(data, "output", "")
            handle = _json_object(output) if call_id in dispatch_args else {}
            handle_id = handle.get("handle_id")
            if handle.get("status") == "in_progress" and isinstance(handle_id, str):
                arguments = dispatch_args[call_id]
                tool = handle.get("tool_name") or arguments.get("tool")
                opened[handle_id] = {
                    "task_id": handle_id,
                    "tool": tool if isinstance(tool, str) else _DISPATCH_TOOL,
                    "since": item.created_at,
                    "detail": _detail(arguments),
                }
            else:
                _close_mentioned(opened, output)
        elif item.type == "message":
            _close_mentioned(opened, _message_text(getattr(data, "content", None)))
    return list(opened.values())


def _close_mentioned(opened: dict[str, dict[str, Any]], text: str) -> None:
    """Drop tasks whose completion line appears in ``text``."""
    if not opened or "task " not in text:
        return
    for task_id in list(opened):
        if re.search(rf"task {re.escape(task_id)} (?:{_CLOSED})", text):
            del opened[task_id]

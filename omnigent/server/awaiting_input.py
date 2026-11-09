"""Per-session "awaiting your input" signal, stored as a session label.

The label ``omnigent.awaiting_input`` holds a short reason while the session is
waiting on the user for something other than a formal approval dialog: the
agent ended its turn by asking a question, or a native TUI reported a
``Notification`` hook. An empty value means not awaiting (labels are
upsert-only).
"""

from __future__ import annotations

import re

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.entities import MessageData
from omnigent.server import session_live_state
from omnigent.stores.conversation_store import ConversationStore

AWAITING_INPUT_LABEL_KEY = "omnigent.awaiting_input"

_FENCED_CODE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_TRAILING_MARKUP = " \t*_)\"'”`"
# Direct requests that need no question mark, e.g. "Let me know when ready."
_REQUEST_PHRASES = re.compile(
    r"\b(?:should i|do you want|would you like|want me to|let me know|please confirm"
    r"|approve|which\b.{0,60}\bdo you)\b",
    re.IGNORECASE,
)


def _strip_code_and_quotes(text: str) -> str:
    """Drop fenced code blocks and markdown block quotes, which never ask the user."""
    without_code = _FENCED_CODE.sub("", text)
    return "\n".join(
        line for line in without_code.splitlines() if not line.lstrip().startswith(">")
    )


def _lead_paragraph(text: str) -> str:
    """Return the last paragraph, skipping trailing option lists to the lead-in."""
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    while len(paragraphs) > 1 and all(
        _LIST_LINE.match(line) for line in paragraphs[-1].splitlines()
    ):
        paragraphs.pop()
    return paragraphs[-1] if paragraphs else ""


def detect_awaiting_input(text: str) -> str | None:
    """
    Return the user-directed request that ends an assistant message, if any.

    Only the final sentence of the last paragraph counts, so a rhetorical
    question followed by a statement does not. Code blocks and quoted lines are
    ignored.

    :param text: Full assistant message text.
    :returns: The asking sentence (at most ``LABEL_VALUE_MAX_LEN`` chars), or ``None``.
    """
    paragraph = " ".join(_lead_paragraph(_strip_code_and_quotes(text)).split())
    if not paragraph:
        return None
    sentences = [part for part in _SENTENCE_BREAK.split(paragraph) if part]
    last = sentences[-1].lstrip("*_` ").rstrip(_TRAILING_MARKUP)
    if not (last.endswith("?") or _REQUEST_PHRASES.search(last)):
        return None
    if len(last) > LABEL_VALUE_MAX_LEN:
        return "…" + last[-(LABEL_VALUE_MAX_LEN - 1) :]
    return last


def _latest_assistant_text(store: ConversationStore, session_id: str) -> str:
    """Return the newest visible message's text when it is the assistant's, else ``""``."""
    page = store.list_items(session_id, limit=5, order="desc", type="message")
    for item in page.data:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        if item.data.role != "assistant":
            return ""
        return "\n".join(
            block["text"]
            for block in item.data.content
            if block.get("type") == "output_text" and isinstance(block.get("text"), str)
        )
    return ""


def set_awaiting_input(store: ConversationStore, session_id: str, reason: str) -> None:
    """Write the awaiting-input reason (empty clears it); unchanged values skip the write."""
    reason = reason[:LABEL_VALUE_MAX_LEN]
    conv = store.get_conversation(session_id)
    if conv is None or conv.labels.get(AWAITING_INPUT_LABEL_KEY, "") == reason:
        return
    store.set_labels(session_id, {AWAITING_INPUT_LABEL_KEY: reason})


def refresh_after_turn(store: ConversationStore, session_id: str) -> None:
    """Set or clear the label from the turn's final assistant message."""
    reason = detect_awaiting_input(_latest_assistant_text(store, session_id)) or ""
    set_awaiting_input(store, session_id, reason)


def schedule_refresh_after_turn(session_id: str) -> None:
    """Evaluate the finished turn off-loop on the live-state worker."""
    store = session_live_state.conversation_store()
    if store is not None:
        session_live_state.submit("awaiting_input", refresh_after_turn, store, session_id)


def schedule_clear(session_id: str) -> None:
    """Clear the label off-loop on the live-state worker."""
    store = session_live_state.conversation_store()
    if store is not None:
        session_live_state.submit("awaiting_input", set_awaiting_input, store, session_id, "")

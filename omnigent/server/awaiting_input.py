"""Per-session "awaiting your input" signal, stored as a session label.

The label ``omnigent.awaiting_input`` holds a short reason while the session is
waiting on the user for something other than a formal approval dialog: the
agent ended its turn by asking a question, or a native TUI reported a
``Notification`` hook. An empty value means not awaiting (labels are
upsert-only).

``omnigent.input_answered`` records that the wait was answered (a reply, or an
approval resolved) until the agent's next turn ends, so the inbox can tell
"input given" apart from "needs input" and from plain read state.

``omnigent.awaiting_ack`` holds what to acknowledge when the turn handed over a
result; the inbox shows it while unread, so reading it is the acknowledgement.
With a classifier key set, the turn's final message is classified by
``reply_classifier`` and the regex heuristic is the fallback.
"""

from __future__ import annotations

import functools
import logging
import re

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.entities import MessageData
from omnigent.server import reply_classifier, session_live_state
from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

AWAITING_INPUT_LABEL_KEY = "omnigent.awaiting_input"
INPUT_ANSWERED_LABEL_KEY = "omnigent.input_answered"
AWAITING_ACK_LABEL_KEY = "omnigent.awaiting_ack"
# The answered label for an approval, which has no question text of its own.
ANSWERED_APPROVAL = "approval"

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


# Prefix of a message another session sent (see ``session_orchestration``).
_AGENT_MESSAGE_PREFIX = "[Message from "


def _message_text(data: MessageData) -> str:
    return "\n".join(
        block["text"]
        for block in data.content
        if block.get("type") in ("output_text", "input_text")
        and isinstance(block.get("text"), str)
    )


def _latest_assistant_text(store: ConversationStore, session_id: str) -> str:
    """
    Return the newest visible message's text when it is the assistant's, else ``""``.

    A turn that answered another session's message returns ``""``: its closing
    question is for that agent, not the user.
    """
    page = store.list_items(session_id, limit=20, order="desc", type="message")
    final = ""
    for item in page.data:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        if item.data.role == "assistant":
            final = final or _message_text(item.data) or " "
            continue
        if not final:
            return ""
        if _message_text(item.data).startswith(_AGENT_MESSAGE_PREFIX):
            return ""
        return final.strip()
    return final.strip()


def _is_subagent(labels: dict[str, str]) -> bool:
    """A native subagent session talks to its parent agent, never to the user."""
    return labels.get(WRAPPER_LABEL_KEY, "").endswith("-subagent")


def set_awaiting_input(store: ConversationStore, session_id: str, reason: str) -> None:
    """Write the awaiting-input reason (empty clears it); unchanged values skip the write."""
    reason = reason[:LABEL_VALUE_MAX_LEN]
    conv = store.get_conversation(session_id)
    if conv is None or conv.labels.get(AWAITING_INPUT_LABEL_KEY, "") == reason:
        return
    if reason and _is_subagent(conv.labels):
        return
    store.set_labels(session_id, {AWAITING_INPUT_LABEL_KEY: reason})


def _write(
    store: ConversationStore,
    session_id: str,
    awaiting: str,
    answered: str,
    ack: str | None = None,
) -> None:
    """Upsert the changed labels; ``ack=None`` leaves the acknowledgement as is."""
    conv = store.get_conversation(session_id)
    if conv is None:
        return
    values = {AWAITING_INPUT_LABEL_KEY: awaiting, INPUT_ANSWERED_LABEL_KEY: answered}
    if ack is not None:
        values[AWAITING_ACK_LABEL_KEY] = ack
    updates = {
        key: value[:LABEL_VALUE_MAX_LEN]
        for key, value in values.items()
        if conv.labels.get(key, "") != value[:LABEL_VALUE_MAX_LEN]
    }
    if updates:
        store.set_labels(session_id, updates)


def _apply_heuristic(store: ConversationStore, session_id: str, text: str) -> None:
    _write(store, session_id, detect_awaiting_input(text) or "", "", "")


def apply_classification(
    store: ConversationStore,
    session_id: str,
    text: str,
    result: reply_classifier.ReplyClass | None,
) -> None:
    """Write a classification of ``text``, unless the session has moved on since."""
    if _latest_assistant_text(store, session_id) != text:
        return
    if result is None:
        _apply_heuristic(store, session_id, text)
        return
    awaiting = (result.reason or "Needs your input") if result.kind == "input" else ""
    ack = (result.reason or "Ready for review") if result.kind == "ack" else ""
    _logger.info(
        "reply classifier session=%s kind=%s heuristic=%s",
        session_id,
        result.kind,
        "input" if detect_awaiting_input(text) else "none",
    )
    _write(store, session_id, awaiting, "", ack)


def refresh_after_turn(store: ConversationStore, session_id: str) -> None:
    """Set or clear the labels from the turn's final assistant message; the answer is consumed."""
    conv = store.get_conversation(session_id)
    if conv is not None and _is_subagent(conv.labels):
        _write(store, session_id, "", "", "")
        return
    text = _latest_assistant_text(store, session_id)
    if not text or not reply_classifier.enabled():
        _apply_heuristic(store, session_id, text)
        return
    _write(store, session_id, "", "", "")

    def write(result: reply_classifier.ReplyClass | None) -> None:
        session_live_state.submit(
            "awaiting_input", apply_classification, store, session_id, text, result
        )

    reply_classifier.submit(text, write, on_failure=lambda: write(None))


def mark_answered(store: ConversationStore, session_id: str, *, approval: bool) -> None:
    """Move a pending wait to answered: the awaiting question, or an approval resolved."""
    conv = store.get_conversation(session_id)
    if conv is None:
        return
    question = conv.labels.get(AWAITING_INPUT_LABEL_KEY, "")
    if question or approval:
        _write(store, session_id, "", question or ANSWERED_APPROVAL)


def clear(store: ConversationStore, session_id: str) -> None:
    """Drop the awaiting, answered and acknowledgement labels."""
    _write(store, session_id, "", "", "")


def schedule_refresh_after_turn(session_id: str) -> None:
    """Evaluate the finished turn off-loop on the live-state worker."""
    store = session_live_state.conversation_store()
    if store is not None:
        session_live_state.submit("awaiting_input", refresh_after_turn, store, session_id)


def schedule_clear(session_id: str) -> None:
    """Clear the awaiting and answered labels off-loop on the live-state worker."""
    store = session_live_state.conversation_store()
    if store is not None:
        session_live_state.submit("awaiting_input", clear, store, session_id)


def schedule_answered(session_id: str, *, approval: bool = False) -> None:
    """Mark the wait answered off-loop on the live-state worker."""
    store = session_live_state.conversation_store()
    if store is not None:
        answered = functools.partial(mark_answered, approval=approval)
        session_live_state.submit("awaiting_input", answered, store, session_id)

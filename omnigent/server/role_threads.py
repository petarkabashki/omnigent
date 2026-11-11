"""Role threads: a per-counterpart conversation index kept as labels on the role holder.

A thread is a tag on peer messages plus ``thread.<hash>.<field>`` labels on the
session holding the role. The transcript stays the ledger; the labels are only
the index ``sys_thread_list`` reads, so they survive compaction and relaunch.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.loop_guard import (
    HOP_RESET_SECONDS,
    GuardVerdict,
    evaluate,
    limit_reached,
)
from omnigent.server import reply_classifier, session_live_state
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
)

THREAD_ROLE_LABEL = "thread.role"  # handle of the role a session held
THREAD_OF_LABEL = "thread.of"  # on an owner-started side chat: its counterpart
PROMOTION_MESSAGE_THRESHOLD = 20  # messages after which a thread is a promotion candidate
THREAD_STATES = ("open", "waiting_on_them", "waiting_on_us", "waiting_on_owner", "blocked", "done")
OWNER_SENDER = "owner"  # sender name on a message the owner posted into a thread
HUB_SENDER = "hub"  # sender name on a hub reminder about an unhandled thread
COUNTERPART_MAX_LEN = 200

_SCAN_PAGES = 100  # newest 10,000 messages per session
_INDEX_LABEL = re.compile(r"thread\.([0-9a-f]{12})\.([a-z0-9_]+)")
_THREAD_TAG = re.compile(r"^\[Message from [^\n]*?\] \[thread ([^\]\n]+)\]\n", re.ASCII)
_URGENT_STATES = {"waiting_on_owner": 0, "blocked": 0, "waiting_on_us": 1}


def thread_hash(counterpart: str) -> str:
    """Short stable label-key segment for a counterpart (keeps keys under 128 chars)."""
    return hashlib.sha256(counterpart.encode()).hexdigest()[:12]


def _label(counterpart: str, field: str) -> str:
    return f"thread.{thread_hash(counterpart)}.{field}"


THREAD_NOTE_PREFIX = "[thread note] "  # a side chat's note line to its own holder


def validate_counterpart(value: object) -> str:
    """A counterpart key that is safe to put in a header and a label value."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > COUNTERPART_MAX_LEN
        or any(char in value for char in "[]\n\r")
    ):
        raise OmnigentError(
            "Provide a counterpart handle such as 'project/Role'", code=ErrorCode.INVALID_INPUT
        )
    return value.strip()


def holds_role(session: Conversation) -> bool:
    """Whether the session currently holds a role."""
    return bool(session.labels.get(ALIAS_LABEL_KEY))


def inbound_thread_key(source: Conversation, sender_handle: str | None) -> str:
    """The sender's handle, or the owner-thread it speaks for, else ``session:<id>``."""
    return source.labels.get(THREAD_OF_LABEL) or sender_handle or f"session:{source.id}"


def preview(text: object) -> str:
    """One-line summary that fits a label value."""
    line = " ".join(str(text).split())
    return line if len(line) <= LABEL_VALUE_MAX_LEN else line[: LABEL_VALUE_MAX_LEN - 1] + "…"


def thread_index(labels: Mapping[str, str]) -> list[dict[str, Any]]:
    """Thread rows from a holder's labels, urgent first, then most recent."""
    grouped: dict[str, dict[str, str]] = {}
    for key, value in labels.items():
        match = _INDEX_LABEL.fullmatch(key)
        if match:
            grouped.setdefault(match.group(1), {})[match.group(2)] = value
    rows = []
    for fields in grouped.values():
        if "cp" not in fields:
            continue
        messages, unread = _to_int(fields.get("n")), _to_int(fields.get("unread"))
        rows.append(
            {
                "counterpart": fields["cp"],
                "state": fields.get("state", "open"),
                "last_message_id": fields.get("last"),
                "unread": unread,
                "messages": messages,
                "summary": fields.get("sum", ""),
                "updated_at": _to_int(fields.get("at")),
                "promotion_candidate": messages >= PROMOTION_MESSAGE_THRESHOLD,
                "held": _held_summary(fields),
                # Messages to this role deleted with a holder while it was vacant.
                "dropped": _to_int(fields.get("dropped")),
            }
        )
    rows.sort(key=lambda row: -row["updated_at"])
    rows.sort(key=lambda row: _URGENT_STATES.get(row["state"], 2))
    return rows


def _to_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _row_fields(labels: Mapping[str, str], counterpart: str) -> dict[str, str]:
    prefix = f"thread.{thread_hash(counterpart)}."
    return {key[len(prefix) :]: value for key, value in labels.items() if key.startswith(prefix)}


def thread_hops(labels: Mapping[str, str], counterpart: str) -> int:
    """The loop hop count currently on a thread row."""
    return _to_int(_row_fields(labels, counterpart).get("hops"))


def _held_summary(fields: Mapping[str, str]) -> dict[str, Any] | None:
    if "held_to" not in fields:
        return None
    return {
        "text": fields.get("held", ""),
        "held_to": fields["held_to"],
        "held_at": _to_int(fields.get("held_at")),
    }


def guard_outbound(
    sender: Conversation, counterpart: str, text: str, *, now: int | None = None
) -> GuardVerdict:
    """Counters after a message from a role holder to ``counterpart`` (see ``loop_guard``)."""
    fields = _row_fields(sender.labels, counterpart)
    return evaluate(
        text,
        prev_hops=_to_int(fields.get("hops")),
        last_at=_to_int(fields.get("at")),
        stamps=[_to_int(part) for part in fields.get("times", "").split(",") if part],
        recent=[part for part in fields.get("recent", "").split(",") if part],
        now=now if now is not None else int(time.time()),
    )


def _guard_labels(
    counterpart: str, verdict: GuardVerdict | None, *, recent: bool
) -> dict[str, str]:
    """Burst stamps are mirrored on both sides; recent digests are the sender's own."""
    if verdict is None:
        return {}
    labels = {_label(counterpart, "times"): ",".join(str(stamp) for stamp in verdict.stamps)}
    if recent:
        labels[_label(counterpart, "recent")] = ",".join(verdict.recent)
    return labels


def loop_guard_error(
    verdict: GuardVerdict, sender_key: str, counterpart: str, *, replaced: bool
) -> OmnigentError:
    """The error a sender gets when its message was held for the owner."""
    limit = limit_reached(verdict.tripped or "loop")
    return OmnigentError(
        f"Thread loop guard: held for the owner. {sender_key} \u2194 {counterpart} hit {limit}. "
        "Don't resend; the owner will release it. The count resets after "
        f"{HOP_RESET_SECONDS // 60} quiet minutes, an owner message, or the owner's Allow more."
        + (" This replaces the message held earlier." if replaced else ""),
        code=ErrorCode.CONFLICT,
    )


_HELD_FIELDS = ("held", "held_to", "held_at", "held_src", "held_id", "held_n")
_HELD_CHUNK = LABEL_VALUE_MAX_LEN


def _held_chunk_key(counterpart: str, index: int) -> str:
    return _label(counterpart, f"heldtext_{index:02d}")


def hold_message(
    store: ConversationStore,
    sender: Conversation,
    counterpart: str,
    *,
    text: str,
    target_id: str,
    source_id: str,
    message_id: str | None,
    sender_handle: str | None,
    now: int | None = None,
) -> bool:
    """Keep a refused message on the sender's thread row; ``True`` if it replaced one.

    The text is split over ``heldtext_NN`` labels (a label value is capped), so it
    survives whole. The thread is marked as waiting on the owner.
    """
    labels = store.get_conversation(sender.id).labels  # type: ignore[union-attr]
    replaced = "held_to" in _row_fields(labels, counterpart)
    if replaced:
        clear_held(store, sender.id, counterpart)
    chunks = [text[i : i + _HELD_CHUNK] for i in range(0, len(text), _HELD_CHUNK)]
    updates = {
        _label(counterpart, "cp"): counterpart,
        _label(counterpart, "state"): "waiting_on_owner",
        _label(counterpart, "sum"): preview(f"held for owner: {text}"),
        _label(counterpart, "held"): preview(text),
        _label(counterpart, "held_to"): target_id,
        _label(counterpart, "held_at"): str(now if now is not None else int(time.time())),
        _label(counterpart, "held_src"): source_id,
        _label(counterpart, "held_n"): str(len(chunks)),
    }
    if message_id:
        updates[_label(counterpart, "held_id")] = message_id
    for index, chunk in enumerate(chunks):
        updates[_held_chunk_key(counterpart, index)] = chunk
    if sender_handle:
        updates[THREAD_ROLE_LABEL] = sender_handle
    store.set_labels(sender.id, updates)
    return replaced


def held_message(labels: Mapping[str, str], counterpart: str) -> dict[str, Any] | None:
    """The full held message on a holder's thread row, or ``None``."""
    fields = _row_fields(labels, counterpart)
    if "held_to" not in fields:
        return None
    count = _to_int(fields.get("held_n"))
    text = "".join(fields.get(f"heldtext_{index:02d}", "") for index in range(count))
    return {
        "text": text,
        "target_id": fields["held_to"],
        "source_id": fields.get("held_src", ""),
        "message_id": fields.get("held_id"),
    }


def clear_held(store: ConversationStore, holder_id: str, counterpart: str) -> None:
    """Drop a thread's held message."""
    labels = store.get_conversation(holder_id).labels  # type: ignore[union-attr]
    prefix = f"thread.{thread_hash(counterpart)}."
    for key in labels:
        field = key[len(prefix) :] if key.startswith(prefix) else ""
        if field in _HELD_FIELDS or field.startswith("heldtext_"):
            store.delete_label(holder_id, key)


def reset_counters(store: ConversationStore, holder_id: str, counterpart: str) -> None:
    """Restart the loop and burst counters on one side of a pair."""
    labels = store.get_conversation(holder_id).labels  # type: ignore[union-attr]
    fields = _row_fields(labels, counterpart)
    if _to_int(fields.get("hops")):
        store.set_labels(holder_id, {_label(counterpart, "hops"): "0"})
    if fields.get("times"):
        store.delete_label(holder_id, _label(counterpart, "times"))


def threads_with_counters(labels: Mapping[str, str]) -> list[str]:
    """Counterparts whose loop or burst counters are running on this holder."""
    return [
        row["counterpart"] for row in thread_index(labels) if _counting(labels, row["counterpart"])
    ]


def _counting(labels: Mapping[str, str], counterpart: str) -> bool:
    fields = _row_fields(labels, counterpart)
    return bool(_to_int(fields.get("hops")) or fields.get("times"))


def record_inbound(
    store: ConversationStore,
    holder: Conversation,
    counterpart: str,
    holder_handle: str | None,
    message_id: str | None,
    text: object,
    hops: int,
    *,
    keep_state: bool = False,
    count: bool = True,
    verdict: GuardVerdict | None = None,
    now: int | None = None,
) -> bool:
    """Update the holder's index for a delivered message; ``False`` for a duplicate id.

    ``keep_state`` leaves state and summary as the thread's side chat noted them;
    ``count=False`` leaves the message and unread counts alone.
    """
    fields = _row_fields(store.get_conversation(holder.id).labels, counterpart)  # type: ignore[union-attr]
    if message_id and fields.get("last") == message_id:
        return False
    updates = {
        _label(counterpart, "cp"): counterpart,
        _label(counterpart, "state"): "waiting_on_us",
        _label(counterpart, "unread"): str(_to_int(fields.get("unread")) + 1),
        _label(counterpart, "n"): str(_to_int(fields.get("n")) + 1),
        _label(counterpart, "sum"): preview(text),
        _label(counterpart, "hops"): str(hops),
        _label(counterpart, "at"): str(now if now is not None else int(time.time())),
    }
    updates.update(_guard_labels(counterpart, verdict, recent=False))
    if keep_state:
        del updates[_label(counterpart, "state")], updates[_label(counterpart, "sum")]
    if not count:
        del updates[_label(counterpart, "unread")], updates[_label(counterpart, "n")]
    if message_id:
        updates[_label(counterpart, "last")] = message_id
    if holder_handle:
        updates[THREAD_ROLE_LABEL] = holder_handle
    store.set_labels(holder.id, updates)
    return True


def settle_inbound_kind(
    store: ConversationStore,
    holder_id: str,
    counterpart: str,
    message_id: str,
    kind: reply_classifier.ReplyKind,
) -> None:
    """An inbound message that needs nothing leaves its thread open, not waiting on us."""
    conv = store.get_conversation(holder_id)
    if conv is None or kind != "info":
        return
    fields = _row_fields(conv.labels, counterpart)
    if fields.get("last") == message_id and fields.get("state") == "waiting_on_us":
        store.set_labels(holder_id, {_label(counterpart, "state"): "open"})


def schedule_inbound_classification(
    holder_id: str, counterpart: str, message_id: str | None, text: object
) -> None:
    """Classify a delivered thread message off-loop and settle the thread's state."""
    store = session_live_state.conversation_store()
    if store is None or not message_id or not isinstance(text, str):
        return
    if not reply_classifier.enabled():
        return

    def settle(result: reply_classifier.ReplyClass) -> None:
        session_live_state.submit(
            "role_threads",
            settle_inbound_kind,
            store,
            holder_id,
            counterpart,
            message_id,
            result.kind,
        )

    reply_classifier.submit(text, settle, audience="agent")


def _reset_nudges(counterpart: str) -> dict[str, str]:
    """Labels that clear a thread's reminder count once it is handled."""
    return {_label(counterpart, "nudges"): "0", _label(counterpart, "nudged_at"): "0"}


def record_outbound(
    store: ConversationStore,
    sender: Conversation,
    counterpart: str,
    sender_handle: str | None,
    message_id: str | None,
    text: object,
    hops: int,
    *,
    verdict: GuardVerdict | None = None,
    now: int | None = None,
) -> None:
    """A role holder wrote to ``counterpart``: waiting on them, nothing unread."""
    fields = _row_fields(store.get_conversation(sender.id).labels, counterpart)  # type: ignore[union-attr]
    updates = {
        _label(counterpart, "cp"): counterpart,
        _label(counterpart, "state"): "waiting_on_them",
        _label(counterpart, "unread"): "0",
        _label(counterpart, "n"): str(_to_int(fields.get("n")) + 1),
        _label(counterpart, "sum"): preview(f"sent: {text}"),
        _label(counterpart, "hops"): str(hops),
        _label(counterpart, "at"): str(now if now is not None else int(time.time())),
        **_reset_nudges(counterpart),
    }
    updates.update(_guard_labels(counterpart, verdict, recent=True))
    if message_id:
        updates[_label(counterpart, "last")] = message_id
    if sender_handle:
        updates[THREAD_ROLE_LABEL] = sender_handle
    store.set_labels(sender.id, updates)


def record_dropped(store: ConversationStore, sender_id: str, counterpart: str) -> None:
    """Count one message to ``counterpart`` that was dropped before any holder got it.

    Unlocked read-modify-write: concurrent drops can lose a count, which is fine for a hint.
    """
    sender = store.get_conversation(sender_id)
    if sender is None:
        return
    dropped = _to_int(_row_fields(sender.labels, counterpart).get("dropped")) + 1
    store.set_labels(
        sender_id,
        {_label(counterpart, "cp"): counterpart, _label(counterpart, "dropped"): str(dropped)},
    )


def note_thread(
    store: ConversationStore,
    holder: Conversation,
    counterpart: str,
    *,
    state: str | None,
    summary: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    """Set a thread's state and/or summary on the holder; returns the thread's row."""
    if state is not None and state not in THREAD_STATES:
        raise OmnigentError(
            f"state must be one of {', '.join(THREAD_STATES)}", code=ErrorCode.INVALID_INPUT
        )
    # Noting a thread means the role has seen it, so nothing in it is unread.
    updates = {
        _label(counterpart, "cp"): counterpart,
        _label(counterpart, "at"): str(now if now is not None else int(time.time())),
        _label(counterpart, "unread"): "0",
        **_reset_nudges(counterpart),
    }
    if state is not None:
        updates[_label(counterpart, "state")] = state
    if summary is not None:
        updates[_label(counterpart, "sum")] = preview(summary)
    store.set_labels(holder.id, updates)
    labels = store.get_conversation(holder.id).labels  # type: ignore[union-attr]
    return next(row for row in thread_index(labels) if row["counterpart"] == counterpart)


def owner_thread_message(message: object, thread: str | None, message_id: str) -> dict[str, Any]:
    """A message the owner posts into a thread, as the ``data`` of a ``message`` event."""
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise OmnigentError("Provide a message of 1–8000 characters", code=ErrorCode.INVALID_INPUT)
    tag = f" [thread {thread}]" if thread else ""
    header = f"[Message from {OWNER_SENDER} (message {message_id})]{tag}"
    return {"role": "user", "content": [{"type": "input_text", "text": f"{header}\n{message}"}]}


def settle_owner_wait(store: ConversationStore, holder: Conversation, counterpart: str) -> None:
    """The owner answered in this thread, so it no longer waits on them."""
    fields = _row_fields(holder.labels, counterpart)
    if fields.get("state") == "waiting_on_owner":
        store.set_labels(holder.id, {_label(counterpart, "state"): "open"})


def _from_owner(row: Mapping[str, Any]) -> bool:
    return str(row.get("header", "")).startswith(f"[Message from {OWNER_SENDER} (")


def thread_of_header(text: str) -> str | None:
    """The thread key a delivered message header carries, if any."""
    match = _THREAD_TAG.match(text)
    return match.group(1) if match else None


def find_thread_side_chat(
    store: ConversationStore, holder_id: str, counterpart: str, *, user_id: str | None
) -> Conversation | None:
    """The newest live side chat already opened on ``holder_id`` for ``counterpart``."""
    after: str | None = None
    for _ in range(20):
        page = store.list_conversations(
            limit=100, after=after, owned_by=user_id, has_label=THREAD_OF_LABEL, kind=None
        )
        for session in page.data:
            labels = session.labels
            if (
                labels.get(THREAD_OF_LABEL) == counterpart
                and labels.get(SIDE_CHAT_SOURCE_LABEL_KEY) == holder_id
            ):
                return session
        if not page.has_more:
            return None
        after = page.last_id
    return None


def _role_sessions(
    store: ConversationStore, role: str, user_id: str | None
) -> dict[str, Conversation]:
    """Every session that held ``role``, by its ``thread.role`` label."""
    sessions: dict[str, Conversation] = {}
    after: str | None = None
    for _ in range(20):
        page = store.list_conversations(
            limit=100,
            after=after,
            owned_by=user_id,
            has_label=THREAD_ROLE_LABEL,
            include_archived=True,
            kind=None,
        )
        for session in page.data:
            if session.labels.get(THREAD_ROLE_LABEL) == role:
                sessions.setdefault(session.id, session)
        if not page.has_more:
            break
        after = page.last_id
    return sessions


def _thread_keys(store: ConversationStore, counterpart: str, user_id: str | None) -> set[str]:
    """Keys one counterpart's messages carry: its handle and, for a role,
    ``session:<id>`` of each session that held it (sends that reached it before it took it).

    A session that later released the role still contributes all its ``session:<id>`` rows.
    """
    keys = {counterpart}
    if not counterpart.startswith("session:"):
        keys.update(f"session:{sid}" for sid in _role_sessions(store, counterpart, user_id))
    return keys


def _scan_messages(
    store: ConversationStore,
    session_ids: Iterable[str],
    matches: Callable[[str], bool],
    limit: int,
) -> list[dict[str, Any]]:
    """Up to ``limit`` newest messages per session whose text ``matches``, oldest first."""
    found: list[dict[str, Any]] = []
    for session_id in session_ids:
        cursor: str | None = None
        hits = 0
        newest_first: list[dict[str, Any]] = []
        for _ in range(_SCAN_PAGES):
            page = store.list_items(
                session_id, limit=100, after=cursor, order="desc", type="message"
            )
            for item in page.data:
                text = _item_text(item.data)
                if text is not None and matches(text):
                    newest_first.append(_history_row(session_id, item.id, item.created_at, text))
                    hits += 1
            if hits >= limit or not page.has_more:
                break
            cursor = page.last_id
        found.extend(reversed(newest_first))
    return found


def _tagged_messages(
    store: ConversationStore, session_ids: Iterable[str], keys: Collection[str], limit: int
) -> list[dict[str, Any]]:
    """Messages in ``session_ids`` delivered with a ``[thread <key>]`` tag for any of ``keys``."""
    return _scan_messages(store, session_ids, lambda text: thread_of_header(text) in keys, limit)


def thread_history(
    store: ConversationStore,
    holder: Conversation,
    counterpart: str,
    *,
    user_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Newest ``limit`` inbound messages tagged with the thread, across all role holders.

    Sessions that held the role are those carrying ``thread.role`` for its handle,
    so the history follows the role, not the current session.
    """
    role = holder.labels.get(THREAD_ROLE_LABEL)
    sessions = {holder.id: holder}
    if role:
        for session_id, session in _role_sessions(store, role, user_id).items():
            sessions.setdefault(session_id, session)
    keys = _thread_keys(store, counterpart, user_id)
    found = _tagged_messages(store, sessions, keys, limit)
    found.sort(key=lambda row: row["created_at"])
    return found[-limit:]


def thread_conversation(
    store: ConversationStore,
    holder: Conversation,
    counterpart: str,
    *,
    user_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """
    Both sides of a thread, oldest first; ``direction`` is ``in``, ``out``, ``owner`` or ``hub``.
    Owner rows add ``to``: ``both``, ``holder`` or ``counterpart``.

    Sent messages are read where they were delivered: on the counterpart role's sessions,
    tagged with the holder's role; on an unroled ``session:<id>``, by the sender header.
    """
    inbound = thread_history(store, holder, counterpart, user_id=user_id, limit=limit)
    rows = [{**row, "direction": "in"} for row in inbound]
    role = holder.labels.get(THREAD_ROLE_LABEL)
    if role and counterpart.startswith("session:"):
        prefix = f"[Message from {role} ("
        owner_prefix = f"[Message from {OWNER_SENDER} ("

        def sent_in_thread(text: str) -> bool:
            if text.startswith(owner_prefix):
                return thread_of_header(text) == role
            return text.startswith(prefix) and thread_of_header(text) is None

        rows += [
            {**row, "direction": "out"}
            for row in _scan_messages(
                store, [counterpart.removeprefix("session:")], sent_in_thread, limit
            )
        ]
    elif role:
        receivers = _role_sessions(store, counterpart, user_id)
        sent_keys = _thread_keys(store, role, user_id)
        rows += [
            {**row, "direction": "out"}
            for row in _tagged_messages(store, receivers, sent_keys, limit)
        ]
    # An owner post shows once, as the owner's, with the sides it reached as ``to``.
    owner_sides: dict[str, set[str]] = {}
    for row in rows:
        if _from_owner(row):
            owner_sides.setdefault(row["message_id"], set()).add(row["direction"])
    seen_owner_posts: set[str] = set()
    merged: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda row: row["created_at"]):
        if _from_owner(row):
            if row["message_id"] in seen_owner_posts:
                continue
            seen_owner_posts.add(row["message_id"])
            sides = owner_sides[row["message_id"]]
            to = "both" if len(sides) > 1 else "holder" if "in" in sides else "counterpart"
            row = {**row, "direction": OWNER_SENDER, "to": to}
        elif str(row.get("header", "")).startswith(f"[Message from {HUB_SENDER} ("):
            row = {**row, "direction": HUB_SENDER}
        merged.append(row)
    return merged[-limit:]


_SUM_FIELDS = ("n", "unread")
_NEWER_FIELDS = ("state", "sum", "last", "at")


def fold_session_thread(
    store: ConversationStore, holder_id: str, session_id: str, handle: str
) -> bool:
    """Merge the holder's ``session:<id>`` row into the ``handle`` row; ``True`` if one moved.

    Counts add up and the newer row's state and summary win; loop counters and a held
    message stay with the handle row when it has them. The source row is removed, so
    folding twice counts nothing twice.
    """
    holder = store.get_conversation(holder_id)
    if holder is None:
        return False
    source_key = f"session:{session_id}"
    source = _row_fields(holder.labels, source_key)
    if "cp" not in source or source_key == handle:
        return False
    target = _row_fields(holder.labels, handle)
    merged = dict(source) if "cp" not in target else dict(target)
    if "cp" in target:
        for field in _SUM_FIELDS:
            merged[field] = str(_to_int(target.get(field)) + _to_int(source.get(field)))
        if _to_int(source.get("at")) > _to_int(target.get("at")):
            merged.update({f: source[f] for f in _NEWER_FIELDS if f in source})
        if "held_to" not in target and "held_to" in source:
            merged.update(
                {f: v for f, v in source.items() if f in _HELD_FIELDS or f.startswith("heldtext_")}
            )
    merged["cp"] = handle
    store.set_labels(holder_id, {_label(handle, field): value for field, value in merged.items()})
    for field in source:
        store.delete_label(holder_id, _label(source_key, field))
    return True


def fold_session_threads(
    store: ConversationStore, session_id: str, handle: str, *, user_id: str | None
) -> int:
    """A session took ``handle``: fold every holder's ``session:<id>`` row for it.

    The session is marked as having held the role, so history finds what it was sent.
    """
    store.set_labels(session_id, {THREAD_ROLE_LABEL: handle})
    cp_label = _label(f"session:{session_id}", "cp")
    folded = 0
    after: str | None = None
    for _ in range(20):
        page = store.list_conversations(
            limit=100, after=after, owned_by=user_id, has_label=cp_label, kind=None
        )
        for session in page.data:
            folded += fold_session_thread(store, session.id, session_id, handle)
        if not page.has_more:
            break
        after = page.last_id
    return folded


def _item_text(data: Any) -> str | None:
    content = getattr(data, "content", None)
    if not isinstance(content, list):
        return None
    parts = [part.get("text") for part in content if isinstance(part, dict)]
    text = "".join(part for part in parts if isinstance(part, str))
    return text or None


_MESSAGE_ID = re.compile(r"message (msg_[0-9a-f]+)")


def message_id_of_header(header: str) -> str | None:
    """The ``msg_<hex>`` id a peer message header carries."""
    found = _MESSAGE_ID.search(header)
    return found.group(1) if found else None


def _history_row(session_id: str, item_id: str, created_at: int, text: str) -> dict[str, Any]:
    header, _, body = text.partition("\n")
    found = _MESSAGE_ID.search(header)
    return {
        "session_id": session_id,
        "item_id": item_id,
        "message_id": found.group(1) if found else None,
        "created_at": created_at,
        "header": header,
        "text": body[:2000],
    }

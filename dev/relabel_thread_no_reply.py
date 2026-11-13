"""
Move reminder-escalated threads from waiting_on_owner to no_reply, once.

Before no_reply existed, the reminder checker escalated unhandled threads to
waiting_on_owner, nudged or because the holder was offline. Such a row has no
held message and unread > 0: a role's own mark or reply zeroes unread, and any
later message sets the state back to waiting_on_us. Other waiting_on_owner
rows are counted and left alone.

Prints counts only. Dry run by default; ``--apply`` writes a backup first and
``--revert BACKUP`` restores rows still marked no_reply. Never run at startup.

    uv run python dev/relabel_thread_no_reply.py --database-uri sqlite:///$HOME/.omnigent/chat.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from omnigent.server import role_threads
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_PAGE_LIMIT = 200


def _thread_sessions(store: ConversationStore) -> list[Any]:
    """Every session with a thread index, archived or holding a role now or not."""
    sessions: list[Any] = []
    after: str | None = None
    while True:
        page = store.list_conversations(
            limit=_PAGE_LIMIT,
            after=after,
            has_label=role_threads.THREAD_ROLE_LABEL,
            kind=None,
            include_archived=True,
        )
        sessions.extend(page.data)
        if not page.has_more:
            return sessions
        after = page.last_id


def classify(labels: dict[str, str]) -> tuple[list[str], int]:
    """
    Split a session's waiting_on_owner rows.

    :returns: The state label keys made by escalation, and how many other
        waiting_on_owner rows (marked by the role, or held) stay as they are.
    """
    escalated: list[str] = []
    kept = 0
    for row in role_threads.thread_index(labels):
        if row["state"] != "waiting_on_owner":
            continue
        if row["held"] is None and row["unread"] > 0:
            escalated.append(role_threads._label(row["counterpart"], "state"))
        else:
            kept += 1
    return escalated, kept


def relabel(store: ConversationStore, *, apply: bool, backup: Path | None) -> dict[str, int]:
    """Count, and with ``apply`` move, escalated rows to no_reply."""
    planned: dict[str, list[str]] = {}
    kept = 0
    for session in _thread_sessions(store):
        keys, session_kept = classify(session.labels or {})
        kept += session_kept
        if keys:
            planned[session.id] = keys
    counts = {
        "escalated_rows": sum(len(keys) for keys in planned.values()),
        "sessions": len(planned),
        "left_alone_rows": kept,
    }
    if apply and planned:
        assert backup is not None
        backup.write_text(json.dumps({"state": "waiting_on_owner", "rows": planned}, indent=1))
        for session_id, keys in planned.items():
            store.set_labels(session_id, dict.fromkeys(keys, role_threads.NO_REPLY_STATE))
    return counts


def revert(store: ConversationStore, backup: Path) -> dict[str, int]:
    """Restore rows from a backup that are still no_reply; later changes win."""
    saved = json.loads(backup.read_text())
    restored = skipped = 0
    for session_id, keys in saved["rows"].items():
        session = store.get_conversation(session_id)
        labels = (session.labels or {}) if session is not None else {}
        current = [key for key in keys if labels.get(key) == role_threads.NO_REPLY_STATE]
        skipped += len(keys) - len(current)
        if current:
            store.set_labels(session_id, dict.fromkeys(current, saved["state"]))
            restored += len(current)
    return {"restored_rows": restored, "skipped_rows": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-uri", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write no_reply (needs --backup)")
    mode.add_argument("--revert", type=Path, metavar="BACKUP", help="undo an --apply")
    parser.add_argument("--backup", type=Path, help="where --apply saves the old values")
    args = parser.parse_args(argv)
    if args.apply and (args.backup is None or args.backup.exists()):
        parser.error("--apply needs --backup naming a file that doesn't exist yet")
    store = SqlAlchemyConversationStore(args.database_uri)
    if args.revert is not None:
        counts = revert(store, args.revert)
    else:
        counts = relabel(store, apply=args.apply, backup=args.backup)
        counts["applied"] = int(args.apply)
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-off relabel of reminder-escalated threads: dry run, apply, revert."""

from __future__ import annotations

import json
from pathlib import Path

from dev.relabel_thread_no_reply import main
from omnigent.server import role_threads
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _row(counterpart: str, **fields: str) -> dict[str, str]:
    return {
        role_threads._label(counterpart, k): v for k, v in {"cp": counterpart, **fields}.items()
    }


def test_dry_run_apply_and_revert(db_uri: str, tmp_path: Path, capsys) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    holder = store.create_conversation(kind="default").id
    store.set_labels(
        holder,
        {
            ALIAS_LABEL_KEY: "Architect",
            role_threads.THREAD_ROLE_LABEL: "Architect",
            **_row("/Escalated", state="waiting_on_owner", unread="2"),
            **_row("/Marked", state="waiting_on_owner", unread="0"),
            **_row("/Held", state="waiting_on_owner", unread="1", held="ok", held_to="s"),
            **_row("/Later", state="waiting_on_owner", unread="1"),
            **_row("/Open", state="open", unread="2"),
        },
    )

    def run(*args: str) -> dict[str, int]:
        assert main(["--database-uri", db_uri, *args]) == 0
        return json.loads(capsys.readouterr().out)

    def state(counterpart: str) -> str:
        labels = store.get_conversation(holder).labels  # type: ignore[union-attr]
        return labels[role_threads._label(counterpart, "state")]

    expected = {"escalated_rows": 2, "sessions": 1, "left_alone_rows": 2}
    assert run() == {**expected, "applied": 0}
    assert state("/Escalated") == "waiting_on_owner"  # a dry run writes nothing

    backup = tmp_path / "relabel.json"
    assert run("--apply", "--backup", str(backup)) == {**expected, "applied": 1}
    assert [state(cp) for cp in ("/Escalated", "/Later", "/Marked", "/Held")] == [
        "no_reply",
        "no_reply",
        "waiting_on_owner",
        "waiting_on_owner",
    ]

    store.set_labels(holder, {role_threads._label("/Later", "state"): "blocked"})
    assert run("--revert", str(backup)) == {"restored_rows": 1, "skipped_rows": 1}
    assert state("/Escalated") == "waiting_on_owner"
    assert state("/Later") == "blocked"  # a change after the relabel wins

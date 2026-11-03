"""The runner's history boundary: the newest compaction or context reset wins."""

from __future__ import annotations

from typing import Any

from omnigent.runner.session_history import split_history_at_boundary


def _message(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _summary_texts(prefix: list[dict[str, Any]]) -> list[str]:
    return [part["text"] for item in prefix for part in item["content"]]


def test_no_boundary_keeps_every_item() -> None:
    items = [_message("a"), _message("b")]

    assert split_history_at_boundary(items) == ([], items)


def test_clean_reset_drops_everything_before_it() -> None:
    after = _message("new")

    prefix, remaining = split_history_at_boundary(
        [_message("old"), {"type": "context_reset", "epoch": 1}, after]
    )

    assert prefix == []
    assert remaining == [after]


def test_reset_with_summary_carries_a_summary_pair() -> None:
    prefix, remaining = split_history_at_boundary(
        [_message("old"), {"type": "context_reset", "epoch": 1, "summary": "carried"}]
    )

    assert [item["role"] for item in prefix] == ["user", "assistant"]
    assert _summary_texts(prefix)[-1] == "carried"
    assert remaining == []


def test_compaction_after_reset_wins() -> None:
    compacted = [_message("compacted state")]
    after = _message("new")

    prefix, remaining = split_history_at_boundary(
        [
            _message("old"),
            {"type": "context_reset", "epoch": 1},
            _message("epoch"),
            {"type": "compaction", "summary": "s", "compacted_messages": compacted},
            after,
        ]
    )

    assert prefix == compacted
    assert remaining == [after]


def test_reset_after_compaction_wins() -> None:
    after = _message("new")

    prefix, remaining = split_history_at_boundary(
        [
            {"type": "compaction", "summary": "old summary"},
            _message("old"),
            {"type": "context_reset", "epoch": 1},
            after,
        ]
    )

    assert prefix == []
    assert remaining == [after]

"""Tests for runtime conversation-history loading."""

from __future__ import annotations

from typing import Any

from omnigent.entities import (
    CompactionData,
    ContextResetData,
    ConversationItem,
    MessageData,
    PagedList,
    SlashCommandData,
)
from omnigent.entities.pagination import paginate_in_memory
from omnigent.runtime.workflow import _load_initial_history


class _ConversationStore:
    """
    Minimal conversation store for history-loader tests.

    :param items: Chronological conversation items returned by
        ``list_items``.
    """

    def __init__(self, items: list[ConversationItem]) -> None:
        self._items = items

    def list_items(
        self,
        conversation_id: str,
        *,
        type: str | None = None,
        order: str = "asc",
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        **kwargs: Any,
    ) -> PagedList[ConversationItem]:
        """
        Return an in-memory page matching ``ConversationStore.list_items``.

        :param conversation_id: Conversation id being queried.
        :param type: Optional item-type filter, e.g. ``"compaction"``.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :param limit: Maximum items in the returned page.
        :param after: Cursor id to start after.
        :param before: Cursor id to stop before.
        :param kwargs: Additional store-specific parameters ignored by
            this fake.
        :returns: Paginated in-memory items.
        """
        del conversation_id, kwargs
        items = self._items
        if after is not None and type is not None:
            # Resolve the cursor by position, as the store does for any item type.
            ids = [item.id for item in items]
            items = items[ids.index(after) + 1 :] if after in ids else items
            after = None
        items = [item for item in items if type is None or item.type == type]
        return paginate_in_memory(
            items,
            lambda item: item.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )


def test_load_initial_history_filters_visible_slash_command_but_keeps_meta_message() -> None:
    """
    Visible command metadata is not LLM content, but hidden skill
    context is.
    """
    slash = ConversationItem(
        id="sc_1",
        type="slash_command",
        status="completed",
        response_id="turn_skill",
        created_at=1,
        data=SlashCommandData(
            agent="test-agent",
            name="grill-me",
            arguments="review this plan",
        ),
    )
    meta = ConversationItem(
        id="msg_meta",
        type="message",
        status="completed",
        response_id="turn_skill",
        created_at=2,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "<skill>hidden</skill>"}],
            is_meta=True,
        ),
    )
    visible = ConversationItem(
        id="msg_visible",
        type="message",
        status="completed",
        response_id="turn_user",
        created_at=3,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "hello"}],
        ),
    )

    loaded = _load_initial_history(
        _ConversationStore([slash, meta, visible]),  # type: ignore[arg-type]
        "conv_123",
    )

    assert [item.id for item in loaded.items] == ["msg_meta", "msg_visible"]
    assert isinstance(loaded.items[0].data, MessageData)
    assert loaded.items[0].data.is_meta is True


def _message(item_id: str, text: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id="turn",
        created_at=1,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _reset(item_id: str, summary: str | None = None) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="context_reset",
        status="completed",
        response_id="reset",
        created_at=1,
        data=ContextResetData(epoch=1, summary=summary),
    )


def _compaction(item_id: str, last_item_id: str) -> ConversationItem:
    return ConversationItem(
        id=item_id,
        type="compaction",
        status="completed",
        response_id="compact",
        created_at=1,
        data=CompactionData(
            summary="compacted", last_item_id=last_item_id, model="m", token_count=1
        ),
    )


def _texts(items: list[ConversationItem]) -> list[str]:
    return [
        item.data.content[0]["text"]  # type: ignore[union-attr, index]
        for item in items
    ]


def test_load_initial_history_starts_at_a_clean_context_reset() -> None:
    """Items before a reset never reach the prompt."""
    items = [_message("m1", "old"), _reset("r1"), _message("m2", "new")]

    loaded = _load_initial_history(_ConversationStore(items), "conv_1")  # type: ignore[arg-type]

    assert [item.id for item in loaded.items] == ["m2"]


def test_load_initial_history_carries_the_reset_summary() -> None:
    """A reset with a summary prepends the synthetic summary pair."""
    items = [_message("m1", "old"), _reset("r1", summary="carry this"), _message("m2", "new")]

    loaded = _load_initial_history(_ConversationStore(items), "conv_1")  # type: ignore[arg-type]

    assert len(loaded.items) == 3
    assert loaded.items[1].data.content[0]["text"] == "carry this"  # type: ignore[union-attr, index]
    assert loaded.items[2].id == "m2"


def test_load_initial_history_ignores_a_compaction_before_the_reset() -> None:
    """A pre-reset compaction cannot pull earlier context back in."""
    items = [
        _message("m1", "old"),
        _compaction("c1", last_item_id="m1"),
        _message("m2", "older"),
        _reset("r1"),
        _message("m3", "new"),
    ]

    loaded = _load_initial_history(_ConversationStore(items), "conv_1")  # type: ignore[arg-type]

    assert [item.id for item in loaded.items] == ["m3"]


def test_load_initial_history_uses_a_compaction_inside_the_reset_epoch() -> None:
    """A compaction after the reset bounds history as usual."""
    items = [
        _message("m1", "old"),
        _reset("r1"),
        _message("m2", "epoch"),
        _compaction("c1", last_item_id="m2"),
        _message("m3", "new"),
    ]

    loaded = _load_initial_history(_ConversationStore(items), "conv_1")  # type: ignore[arg-type]

    assert _texts(loaded.items)[-1] == "new"
    assert "old" not in _texts(loaded.items)
    assert "epoch" not in _texts(loaded.items)

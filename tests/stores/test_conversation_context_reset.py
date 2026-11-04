"""Context-reset boundaries in SqlAlchemyConversationStore."""

from __future__ import annotations

import pytest

from omnigent.entities import ContextResetData, MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    """Exercise both co-located and separate conversation/metadata databases."""
    return request.getfixturevalue(request.param)


def test_append_context_reset_advances_the_epoch(store: SqlAlchemyConversationStore) -> None:
    """Each reset starts the next epoch and records the item before it."""
    conv = store.create_conversation()
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="turn_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )
    last = store.list_items(conv.id, order="desc", limit=1).data[0]

    first = store.append_context_reset(conv.id, response_id="reset_1")
    second = store.append_context_reset(conv.id, response_id="reset_2", summary="carried")

    assert isinstance(first.data, ContextResetData)
    assert isinstance(second.data, ContextResetData)
    assert (first.data.epoch, first.data.last_item_id) == (1, last.id)
    assert (second.data.epoch, second.data.summary) == (2, "carried")
    stored = store.get_conversation(conv.id)
    assert stored is not None and stored.context_epoch == 2


def test_append_context_reset_is_idempotent_per_stable_id(
    store: SqlAlchemyConversationStore,
) -> None:
    """Repeating a stable id returns the first boundary without a new epoch."""
    conv = store.create_conversation()

    first = store.append_context_reset(
        conv.id, response_id="reset_1", stable_id="0123456789abcdef0123456789abcdef"
    )
    again = store.append_context_reset(
        conv.id, response_id="reset_2", stable_id="0123456789abcdef0123456789abcdef"
    )

    assert again.id == first.id
    assert again.deduplicated is True
    assert len(store.list_items(conv.id, type="context_reset").data) == 1
    stored = store.get_conversation(conv.id)
    assert stored is not None and stored.context_epoch == 1


def test_reset_captures_usage_bases_for_the_new_epoch(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation()
    store.set_session_usage(
        conv.id, {"total_cost_usd": 10.0, "policy_cost_usd": 12.0, "input_tokens": 7}
    )

    store.append_context_reset(conv.id, response_id="reset_1")

    current = store.get_conversation(conv.id)
    assert current is not None
    assert current.session_usage["context_reset_base"] == {
        "epoch": 1,
        "total_cost_usd": 10.0,
        "policy_cost_usd": 12.0,
        "input_tokens": 7,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_replayed_reset_does_not_recapture_the_base(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation()
    store.set_session_usage(conv.id, {"total_cost_usd": 1.0})
    store.append_context_reset(conv.id, response_id="reset_1", stable_id="a" * 32)
    store.set_session_usage(conv.id, {"total_cost_usd": 5.0, "context_reset_base": {"epoch": 1}})

    store.append_context_reset(conv.id, response_id="reset_1", stable_id="a" * 32)
    assert store.advance_context_epoch(conv.id, 1) is False

    current = store.get_conversation(conv.id)
    assert current is not None
    assert current.session_usage["context_reset_base"] == {"epoch": 1}


def test_reset_of_an_unused_session_stores_no_usage(store: SqlAlchemyConversationStore) -> None:
    conv = store.create_conversation()

    store.append_context_reset(conv.id, response_id="reset_1")

    current = store.get_conversation(conv.id)
    assert current is not None
    assert not current.session_usage

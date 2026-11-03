"""Tests for externally-forwarded conversation-item parsing."""

import pytest

from omnigent.entities import ConversationItem, MessageData
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.imports import ImportItemInput
from omnigent.server.routes.sessions import _parse_external_conversation_item
from omnigent.server.schemas import SessionEventInput


def test_native_message_stream_id_survives_snapshot_serialization() -> None:
    """The completed item durably identifies the preview stream it replaces."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                "response_id": "turn_1",
                "message_id": "codex:thread_1:turn_1:agentMessage:item_1",
            },
        )
    )

    assert isinstance(parsed.data, MessageData)
    assert parsed.data.stream_message_id == "codex:thread_1:turn_1:agentMessage:item_1"
    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["stream_message_id"] == (
        "codex:thread_1:turn_1:agentMessage:item_1"
    )


def test_native_message_phase_survives_snapshot_serialization() -> None:
    """Codex's commentary/answer label reaches the web UI on reload and live broadcast."""
    parsed = _parse_external_conversation_item(
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "Checking the tests."}],
                    "phase": "commentary",
                },
                "response_id": "turn_1",
            },
        )
    )

    persisted = ConversationItem(
        id="item_1",
        type=parsed.type,
        status="completed",
        response_id=parsed.response_id,
        created_at=1,
        data=parsed.data,
    )
    assert persisted.to_api_dict()["phase"] == "commentary"


def test_context_reset_items_cannot_be_posted_externally() -> None:
    """Only the reset control writes a context boundary."""
    with pytest.raises(OmnigentError) as caught:
        _parse_external_conversation_item(
            SessionEventInput(
                type="external_conversation_item",
                data={"item_type": "context_reset", "item_data": {"epoch": 1}},
            )
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_context_reset_items_cannot_be_imported() -> None:
    with pytest.raises(OmnigentError) as caught:
        ImportItemInput(type="context_reset", response_id="resp_1", data={"epoch": 1}).to_item()

    assert caught.value.code == ErrorCode.INVALID_INPUT

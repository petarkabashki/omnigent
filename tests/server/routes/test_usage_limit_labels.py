"""The usage-limit reset label follows the session's latest failure."""

from __future__ import annotations

import time

import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_CODE_KEY = "omnigent.last_task_error_code"
_RESETS_AT_KEY = "omnigent.last_task_error_resets_at"


@pytest.mark.asyncio
async def test_reset_label_is_set_cleared_and_overwritten(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner-owner")

    async def persist(error: ErrorDetail | None) -> dict[str, str]:
        await sessions_module._persist_session_status_error_labels(conv.id, error, store)
        fresh = store.get_conversation(conv.id)
        assert fresh is not None
        return dict(fresh.labels)

    limit = ErrorDetail(
        code="usage_limit_reached",
        message="You've hit your weekly limit · resets 10pm (Europe/London)",
    )
    labels = await persist(limit)
    assert labels[_CODE_KEY] == "usage_limit_reached"
    assert time.time() < int(labels[_RESETS_AT_KEY]) <= time.time() + 2 * 86400

    # A limit with no readable reset stores no time.
    labels = await persist(
        ErrorDetail(code="usage_limit_reached", message="You've hit your usage limit.")
    )
    assert labels[_RESETS_AT_KEY] == ""

    labels = await persist(limit)
    labels = await persist(ErrorDetail(code="native_turn_error", message="boom"))
    assert labels[_RESETS_AT_KEY] == ""

    labels = await persist(limit)
    labels = await persist(None)
    assert labels[_RESETS_AT_KEY] == ""
    assert labels[_CODE_KEY] == ""

"""The runner's ``reset`` control drops cached context, or refuses natives."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.native_helpers import _harness_spec

_AGENT_ID = "880b5afda28ad55ff74cbeb9b5fc67fb"
_SESSION_ID = "5a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
_IN_PROCESS_SPEC = AgentSpec(
    spec_version=1, name="t", executor=ExecutorSpec(type="omnigent", config={})
)


async def _post_resets(
    spec: AgentSpec,
    session_id: str,
    resets: list[dict[str, Any]],
    *,
    buffered: list[dict[str, Any]] | None = None,
) -> tuple[list[tuple[int, dict[str, object]]], Any, list[str]]:
    """Create a session, seed its caches, post each reset; return responses and app."""
    app, process_manager = await _build_app_for_spec(spec)
    responses: list[tuple[int, dict[str, object]]] = []
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": _AGENT_ID}
        )
        assert created.status_code == 201, created.text
        runner_app._session_histories_ref[session_id] = [{"role": "user", "content": "old"}]
        if buffered is not None:
            app.state.session_message_buffers[session_id] = buffered
        for reset in resets:
            resp = await client.post(
                f"/v1/sessions/{session_id}/events", json={"type": "reset", **reset}
            )
            responses.append((resp.status_code, resp.json()))
    return responses, app, process_manager.released


@pytest.mark.asyncio
async def test_prepare_with_drop_discards_history_and_buffered_input() -> None:
    try:
        [(status, body)], app, released = await _post_resets(
            _IN_PROCESS_SPEC,
            _SESSION_ID,
            [{"phase": "prepare", "pending": "drop"}],
            buffered=[{"type": "message"}],
        )

        assert (status, body) == (200, {"reset": True, "dropped_buffered": 1})
        assert _SESSION_ID not in runner_app._session_histories_ref
        assert _SESSION_ID not in app.state.session_message_buffers
        assert released == []
    finally:
        runner_app._session_histories_ref.pop(_SESSION_ID, None)


@pytest.mark.asyncio
async def test_prepare_with_reject_refuses_buffered_input() -> None:
    try:
        [(status, body)], app, _ = await _post_resets(
            _IN_PROCESS_SPEC,
            _SESSION_ID,
            [{"phase": "prepare", "pending": "reject"}],
            buffered=[{"type": "message"}],
        )

        assert status == 409
        assert body["error"] == "pending_inputs"
        assert _SESSION_ID in runner_app._session_histories_ref
        assert app.state.session_message_buffers[_SESSION_ID] == [{"type": "message"}]
    finally:
        runner_app._session_histories_ref.pop(_SESSION_ID, None)


@pytest.mark.asyncio
async def test_commit_drops_history_cached_after_prepare() -> None:
    try:
        responses, _, _ = await _post_resets(_IN_PROCESS_SPEC, _SESSION_ID, [{"phase": "commit"}])

        assert responses == [(200, {"reset": True, "phase": "commit"})]
        assert _SESSION_ID not in runner_app._session_histories_ref
    finally:
        runner_app._session_histories_ref.pop(_SESSION_ID, None)


@pytest.mark.asyncio
async def test_reset_on_native_session_is_refused() -> None:
    session_id = "6a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    try:
        [(status, body)], _, _ = await _post_resets(
            _harness_spec("claude-native"), session_id, [{"pending": "reject"}]
        )

        assert status == 409
        assert body["error"] == "reset_unsupported"
    finally:
        runner_app._session_histories_ref.pop(session_id, None)

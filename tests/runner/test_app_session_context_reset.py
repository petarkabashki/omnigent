"""The runner's ``reset`` control drops cached context, or refuses natives."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.codex_native import process_registry
from omnigent.runner import app as runner_app
from omnigent.runner.native import claude_reset, orchestration
from omnigent.runner.native.claude_reset import ClaudeNativeReset
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.helpers import NullServerClient
from tests.runner.native_helpers import _harness_spec

_AGENT_ID = "880b5afda28ad55ff74cbeb9b5fc67fb"
_SESSION_ID = "5a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
_IN_PROCESS_SPEC = AgentSpec(
    spec_version=1, name="t", executor=ExecutorSpec(type="omnigent", config={})
)


class _SnapshotServerClient(NullServerClient):
    """Server whose session snapshot names the agent, so the runner can resolve its spec."""

    labels: dict[str, str] = {}

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        del kwargs
        labels = self.labels

        class _Snapshot(NullServerClient._Response):
            def json(self) -> dict[str, Any]:
                return {"id": url.rsplit("/", 1)[-1], "agent_id": _AGENT_ID, "labels": labels}

        return _Snapshot()


async def _post_resets(
    spec: AgentSpec,
    session_id: str,
    resets: list[dict[str, Any]],
    *,
    buffered: list[dict[str, Any]] | None = None,
    create_session: bool = True,
) -> tuple[list[tuple[int, dict[str, object]]], Any, list[str]]:
    """Create a session, seed its caches, post each reset; return responses and app.

    ``create_session=False`` leaves the spec uncached, like a runner just woken.
    """
    app, process_manager = await _build_app_for_spec(spec, server_client=_SnapshotServerClient())
    responses: list[tuple[int, dict[str, object]]] = []
    async with _runner_client(app) as client:
        if create_session:
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
            _harness_spec("pi-native"), session_id, [{"pending": "reject"}]
        )

        assert status == 409
        assert body["error"] == "reset_unsupported"
    finally:
        runner_app._session_histories_ref.pop(session_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
@pytest.mark.parametrize(
    ("create_session", "name_harness"),
    [
        pytest.param(True, False, id="spec-cached"),
        pytest.param(False, True, id="uncached-server-names-harness"),
        pytest.param(False, False, id="uncached-spec-resolved"),
    ],
)
async def test_claude_native_reset_stops_then_relaunches_the_pane(
    monkeypatch: pytest.MonkeyPatch, harness: str, create_session: bool, name_harness: bool
) -> None:
    session_id = "7a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    harness_field = {"harness": harness} if name_harness else {}
    expected_label = {"claude-native": "Claude", "codex-native": "Codex"}[harness]
    phases: list[tuple[str, str, str]] = []

    async def record_prepare(self: ClaudeNativeReset, sid: str) -> None:
        phases.append(("prepare", sid, self.runtime_label))

    async def record_commit(self: ClaudeNativeReset, sid: str) -> None:
        phases.append(("commit", sid, self.runtime_label))

    monkeypatch.setattr(ClaudeNativeReset, "prepare", record_prepare)
    monkeypatch.setattr(ClaudeNativeReset, "commit", record_commit)
    try:
        responses, _, _ = await _post_resets(
            _harness_spec(harness),
            session_id,
            [
                {"phase": "prepare", "pending": "reject", **harness_field},
                {"phase": "commit", **harness_field},
            ],
            create_session=create_session,
        )

        assert [status for status, _ in responses] == [200, 200]
        # Each harness reaches its own reset, not the other's.
        assert phases == [
            ("prepare", session_id, expected_label),
            ("commit", session_id, expected_label),
        ]
    finally:
        runner_app._session_histories_ref.pop(session_id, None)


@pytest.mark.asyncio
async def test_claude_native_reset_holds_launches_and_drops_the_init_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "8a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    app, _ = await _build_app_for_spec(
        _harness_spec("claude-native"), server_client=_SnapshotServerClient()
    )
    envelopes = app.state.session_init_envelopes
    stale_envelope: Any = (0.0, object())
    envelopes[session_id] = stale_envelope
    event = {"type": "reset", "harness": "claude-native"}
    ensure_locks = app.state.native_terminal_ensure_locks["claude"]

    async with _runner_client(app) as client:
        prepared = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={**event, "phase": "prepare", "pending": "reject"},
        )
        assert prepared.status_code == 200, prepared.text
        assert session_id not in envelopes
        assert ensure_locks[session_id].locked()

        # A snapshot fetched before the server's boundary landed.
        envelopes[session_id] = stale_envelope
        committed = await client.post(
            f"/v1/sessions/{session_id}/events", json={**event, "phase": "commit"}
        )
        assert committed.status_code == 200, committed.text

    assert session_id not in envelopes
    assert not ensure_locks[session_id].locked()


async def test_claude_reset_held_past_the_forward_budget_refuses_and_applies_nothing_late(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session_id = "2f7b1c9e4a6d4e0f9b3c8d1a5e7f6c20"
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_reset, "RESET_PREPARE_LOCK_WAIT_S", 0.05)
    forward_timeout_s = 0.5
    app, _ = await _build_app_for_spec(
        _harness_spec("claude-native"), server_client=_SnapshotServerClient()
    )
    ensure_lock = app.state.native_terminal_ensure_locks["claude"].setdefault(
        session_id, asyncio.Lock()
    )
    url = f"/v1/sessions/{session_id}/events"
    prepare = {"type": "reset", "harness": "claude-native", "phase": "prepare"}

    await ensure_lock.acquire()
    async with _runner_client(app) as client:
        forward = asyncio.create_task(client.post(url, json=prepare))
        try:
            answered, _ = await asyncio.wait({forward}, timeout=forward_timeout_s)
            assert answered, "the reset was still queued when the server's forward gave up"
            refused = forward.result()
            assert refused.status_code == 409
            assert refused.json()["error"] == "reset_in_progress"
            assert (
                refused.json()["detail"]
                == "Claude is still starting. Try the reset again in a moment."
            )

            # The launch ends after the server gave up: nothing takes the lock late.
            ensure_lock.release()
            await asyncio.sleep(0.02)  # custom-lint: disable=sleep-then-assert -- negative check
            assert not ensure_lock.locked()

            pressed_again = await client.post(url, json=prepare)
            assert pressed_again.status_code == 200, pressed_again.text
            assert ensure_lock.locked()
        finally:
            forward.cancel()


@pytest.mark.asyncio
async def test_codex_native_reset_stops_the_backend_and_wipes_the_cursor_under_the_launch_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "9a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.prepare_bridge_dir(session_id)
    codex_native_bridge.write_bridge_state(
        bridge_dir,
        codex_native_bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="thread_old",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id="turn_old",
        ),
    )
    closed: list[str] = []

    class _Backend:
        async def close(self) -> None:
            closed.append(session_id)

    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, session_id, _Backend())
    monkeypatch.setitem(orchestration._AUTO_CODEX_BRIDGE_DIRS, session_id, bridge_dir)
    app, _ = await _build_app_for_spec(
        _harness_spec("codex-native"), server_client=_SnapshotServerClient()
    )
    ensure_locks = app.state.native_terminal_ensure_locks["codex"]
    event = {"type": "reset", "harness": "codex-native"}

    async with _runner_client(app) as client:
        prepared = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={**event, "phase": "prepare", "pending": "reject"},
        )

        assert prepared.status_code == 200, prepared.text
        assert closed == [session_id]
        assert codex_native_bridge.read_bridge_state(bridge_dir) is None
        assert ensure_locks[session_id].locked()


@pytest.mark.asyncio
async def test_codex_native_reset_of_a_rotated_session_stops_the_launching_sessions_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A native /clear moved the pane to the rotated session; the app-server and
    # forwarder stay registered to the session that launched them.
    launching_id = "8a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    rotated_id = "7a1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    bridge_dir = codex_native_bridge.prepare_bridge_dir(launching_id)
    closed: list[str] = []
    reaped: list[Path] = []

    class _Backend:
        async def close(self) -> None:
            closed.append(launching_id)

    def reap(state_dir: Path) -> int:
        reaped.append(state_dir)
        return 0

    monkeypatch.setattr(process_registry, "reap_codex_native_processes_for_state_dir", reap)
    forwarder = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, launching_id, _Backend())
    monkeypatch.setitem(orchestration._AUTO_CODEX_BRIDGE_DIRS, launching_id, bridge_dir)
    monkeypatch.setitem(orchestration._AUTO_FORWARDER_TASKS, launching_id, forwarder)
    server_client = _SnapshotServerClient()
    server_client.labels = {codex_native_bridge.CODEX_NATIVE_BRIDGE_ID_LABEL_KEY: launching_id}
    app, _ = await _build_app_for_spec(_harness_spec("codex-native"), server_client=server_client)

    try:
        async with _runner_client(app) as client:
            prepared = await client.post(
                f"/v1/sessions/{rotated_id}/events",
                json={
                    "type": "reset",
                    "harness": "codex-native",
                    "phase": "prepare",
                    "pending": "reject",
                },
            )

            assert prepared.status_code == 200, prepared.text
            assert closed == [launching_id]
            assert forwarder.cancelled()
            assert launching_id not in orchestration._AUTO_CODEX_APP_SERVERS
            assert reaped == [bridge_dir]
            app.state.native_terminal_ensure_locks["codex"][rotated_id].release()
    finally:
        forwarder.cancel()

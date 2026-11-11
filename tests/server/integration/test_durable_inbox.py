"""Messages queued for a session on a local host survive a hub restart."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.datastructures import Headers

from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import pending_inputs
from omnigent.server import startup_inputs
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import (
    RESENT_NOTICE,
    ManagedStartupInputs,
    StartupInputContext,
)
from tests.server.integration.test_managed_launch_failure_teardown import _Env
from tests.server.integration.test_managed_launch_failure_teardown import (
    teardown_env as teardown_env,
)
from tests.server.integration.test_managed_startup_inputs import _TEXT, _create
from tests.server.integration.test_managed_startup_inputs import (
    startup_env as startup_env,
)
from tests.server.integration.test_sessions_native_kickoff_duplicate import (
    _simulate_transcript_forwarder_echo,
)

pytestmark = pytest.mark.asyncio
_PEER_TEXT = "[Message from omnigent/Product] review C12"


def _text(event: SessionEventInput) -> str:
    return event.data["content"][0]["text"]


async def _recover(service: ManagedStartupInputs, env: _Env, session_id: str, host: Any) -> None:
    await service._recover_session(env.app, session_id, host)
    for task in list(service._deliveries.values()):
        await task


async def test_queued_messages_survive_a_restart_in_order_without_redelivery(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env, name=CODEX_NATIVE_CODING_AGENT.agent_name)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/inbox-workspace")
    host = env.app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="local", runners=[]),
        owner=None,
    )
    service = env.app.state.startup_inputs
    peer = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": _PEER_TEXT}]},
    )
    await service.enqueue(
        session_id, peer, user_id=None, headers=Headers({}), first=False, address="Product"
    )
    sent: list[SessionEventInput] = []

    async def dispatch(context: StartupInputContext, session_id: str, event: SessionEventInput):
        assert await context.claim(env.conv_store, session_id)
        sent.append(event)
        return {"queued": True, "pending_id": "pending"}

    service.dispatch = dispatch
    await _recover(service, env, session_id, host)
    await _recover(service, env, session_id, host)
    assert [_text(event) for event in sent] == [_TEXT], "a submitted input waits for its receipt"
    first_id = sent[0].data["stable_id"]

    # A full restart: a new service, an empty receipt index, nothing claimed here.
    monkeypatch.setattr(startup_inputs, "_claimed_here", set())
    pending_inputs.reset_for_tests()
    restarted = ManagedStartupInputs(env.conv_store, service.hosts)
    restarted.dispatch = dispatch
    await _recover(restarted, env, session_id, host)
    assert len(sent) == 2
    assert sent[1].data["stable_id"] == first_id
    assert _text(sent[1]) == f"{RESENT_NOTICE}\n{_TEXT}"
    rows = env.conv_store.list_pending_inputs(session_id)
    assert rows[0].data.startup_options.resent
    assert rows[1].data.startup_options.address == "Product"
    snapshot = (await env.client.get(f"/v1/sessions/{session_id}")).json()
    assert [entry["pending_id"] for entry in snapshot["pending_inputs"]] == [
        pending_inputs.make_pending_id(session_id, row.id) for row in rows
    ]

    await _simulate_transcript_forwarder_echo(env.client, session_id, _text(sent[1]))
    await _recover(restarted, env, session_id, host)
    assert [_text(event) for event in sent[2:]] == [_PEER_TEXT]
    await _simulate_transcript_forwarder_echo(env.client, session_id, _PEER_TEXT)
    assert env.conv_store.list_pending_inputs(session_id) == []

    # Delivered inputs never come back after another restart.
    monkeypatch.setattr(startup_inputs, "_claimed_here", set())
    again = ManagedStartupInputs(env.conv_store, service.hosts)
    again.dispatch = dispatch
    await _recover(again, env, session_id, host)
    assert len(sent) == 3


async def test_managed_hosts_keep_an_uncertain_input_unsent_after_restart(
    startup_env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = startup_env
    session_id = await _create(env, name=CODEX_NATIVE_CODING_AGENT.agent_name)
    host_id = uuid4().hex
    env.conv_store.set_host_id(session_id, host_id, "/tmp/inbox-workspace")
    host = env.app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="managed", runners=[]),
        owner=None,
        registered_with_managed_token=True,
    )
    sent: list[SessionEventInput] = []

    async def dispatch(context: StartupInputContext, session_id: str, event: SessionEventInput):
        assert await context.claim(env.conv_store, session_id)
        sent.append(event)
        return {"queued": True, "pending_id": "pending"}

    service = env.app.state.startup_inputs
    service.dispatch = dispatch
    await _recover(service, env, session_id, host)
    monkeypatch.setattr(startup_inputs, "_claimed_here", set())
    restarted = ManagedStartupInputs(env.conv_store, service.hosts)
    restarted.dispatch = dispatch
    await _recover(restarted, env, session_id, host)
    assert len(sent) == 1

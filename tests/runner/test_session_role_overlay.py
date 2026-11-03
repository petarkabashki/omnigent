"""An aliased session's role reaches the harness through the runner's spec resolver."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_role_resolver, _SessionSnapshot
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

AGENT_ID = "ag_role"
SESSION_ID = "conv_role"


async def _sdk_spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    del agent_id, session_id
    return AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Base instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )


class _AliasedSessionServer(NullServerClient):
    """Serves a session snapshot carrying the ``omnigent.alias`` label."""

    def __init__(self, workspace: str) -> None:
        super().__init__()
        self._workspace = workspace

    class _Resp:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(SESSION_ID):
            return self._Resp(
                {
                    "agent_id": AGENT_ID,
                    "created_at": 0,
                    "workspace": self._workspace,
                    "labels": {"omnigent.alias": "Bench", "omnigent.role_param.repo": "demo"},
                }
            )
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super()._Response()


@pytest.mark.asyncio
async def test_turn_spec_carries_role_text_and_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    role = home / ".omnigent" / "roles" / "Bench.md"
    role.parent.mkdir(parents=True)
    role.write_text(
        "---\nmcp_servers:\n  bench_tools:\n    command: bench-mcp\n"
        "    args: ['${params.repo}']\n---\nBench role for ${params.repo}.\n"
    )
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_sdk_spec_resolver,
        server_client=_AliasedSessionServer(str(tmp_path / "ws")),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        _ = resp.text

    assert harness.posted_bodies, "the turn never reached the harness"
    instructions = harness.posted_bodies[0]["instructions"]
    assert "Base instructions." in instructions
    assert "Bench role for demo." in instructions


@pytest.mark.asyncio
async def test_session_role_resolver_overlays_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    role = home / ".omnigent" / "roles" / "Bench.md"
    role.parent.mkdir(parents=True)
    role.write_text("---\nmcp_servers:\n  bench_tools:\n    command: bench-mcp\n---\nBody.\n")

    def snapshot(labels: dict[str, str]) -> Any:
        async def fetch(session_id: str) -> _SessionSnapshot:
            del session_id
            return _SessionSnapshot(
                ok=True,
                status_code=200,
                created_at=0.0,
                workspace=None,
                agent_id=AGENT_ID,
                labels=labels,
            )

        return fetch

    aliased = _session_role_resolver(_sdk_spec_resolver, snapshot({"omnigent.alias": "Bench"}))
    spec = await aliased(AGENT_ID, SESSION_ID)
    assert isinstance(spec, AgentSpec)
    assert [server.name for server in spec.mcp_servers] == ["bench_tools"]
    # No alias label, or no session id, leaves the resolver's spec untouched.
    plain = _session_role_resolver(_sdk_spec_resolver, snapshot({}))
    assert (await plain(AGENT_ID, SESSION_ID)).mcp_servers == []  # type: ignore[union-attr]
    assert (await aliased(AGENT_ID, None)).mcp_servers == []  # type: ignore[union-attr]

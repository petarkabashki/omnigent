"""Integration tests for ``POST /v1/sessions/{id}/reset`` (in-place context reset).

The server forwards a ``reset`` prepare to the bound runner, which drops its
cached history (200) or, on an older runner, ignores it (204). Only after the
runner agrees is the ``context_reset`` boundary persisted, then a ``commit``
drops anything cached meanwhile.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.server.routes._sessions import reset as reset_module
from omnigent.server.routes._sessions.common import _session_status_cache
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


def _fake_runner(reset_status: int) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Build a mock runner answering ``reset`` with *reset_status*."""
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = (
            json.loads(request.content) if request.method == "POST" and request.content else None
        )
        if isinstance(body, dict) and body.get("type") == "reset":
            captured.append(body)
            return httpx.Response(reset_status, json={"reset": True})
        return httpx.Response(204)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ), captured


_PREPARE = {"type": "reset", "phase": "prepare", "pending": "reject"}
_COMMIT = {"type": "reset", "phase": "commit"}


async def _reset_twice(
    client: httpx.AsyncClient,
    reset_status: int,
    *bodies: dict[str, Any],
    status: str | None = None,
) -> tuple[str, list[httpx.Response], list[dict[str, Any]]]:
    """Create a session and POST each reset body with the fake runner bound."""
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner(reset_status)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        created = await client.post(
            "/v1/sessions", json={"agent_id": agent["id"], "initial_items": []}
        )
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        if status is not None:
            _session_status_cache[sid] = status
        responses = [await client.post(f"/v1/sessions/{sid}/reset", json=b) for b in bodies]
    finally:
        await runner.aclose()
        set_runner_client(None)
        if status is not None:
            _session_status_cache.pop(sid, None)
    return sid, responses, captured


async def _reset_items(client: httpx.AsyncClient, sid: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/v1/sessions/{sid}/items")
    assert resp.status_code == 200, resp.text
    return [item for item in resp.json()["data"] if item["type"] == "context_reset"]


async def test_reset_forwards_and_persists_a_boundary(client: httpx.AsyncClient) -> None:
    sid, (first, second), captured = await _reset_twice(
        client, 200, {}, {"summary": {"text": "carried"}}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert (first.json()["epoch"], second.json()["epoch"]) == (1, 2)
    assert first.json()["session_id"] == sid
    assert captured == [_PREPARE, _COMMIT] * 2
    assert [item["id"] for item in await _reset_items(client, sid)] == [
        first.json()["boundary_item_id"],
        second.json()["boundary_item_id"],
    ]


async def test_reset_idempotency_key_returns_the_first_boundary(
    client: httpx.AsyncClient,
) -> None:
    sid, (first, again), captured = await _reset_twice(
        client, 200, {"idempotency_key": "k1"}, {"idempotency_key": "k1"}
    )

    assert again.json() == first.json()
    assert captured == [_PREPARE, _COMMIT]
    assert len(await _reset_items(client, sid)) == 1


async def test_old_runner_refuses_and_nothing_is_written(client: httpx.AsyncClient) -> None:
    sid, (resp,), _ = await _reset_twice(client, 204, {})

    assert resp.status_code == 409, resp.text
    assert "reset_unsupported" in resp.text
    assert await _reset_items(client, sid) == []


async def test_reset_rejects_auto_summary(client: httpx.AsyncClient) -> None:
    sid, (resp,), captured = await _reset_twice(client, 200, {"summary": "auto"})

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


async def test_reset_of_a_running_session_is_refused(client: httpx.AsyncClient) -> None:
    sid, (resp,), captured = await _reset_twice(client, 200, {}, status="running")

    assert resp.status_code == 409, resp.text
    assert "session_busy" in resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


async def test_message_is_refused_while_a_reset_is_in_flight(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": []}
    )
    sid = created.json()["id"]
    reset_module._reset_in_flight[sid] = reset_module._InFlightReset(stable_id=None)
    try:
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    finally:
        reset_module._reset_in_flight.pop(sid, None)

    assert resp.status_code == 409, resp.text
    assert "reset_in_progress" in resp.text


async def test_external_session_id_from_before_a_reset_is_refused(
    client: httpx.AsyncClient,
) -> None:
    sid, [reset], _ = await _reset_twice(client, 200, {})
    assert reset.status_code == 200, reset.text

    stale = await client.patch(
        f"/v1/sessions/{sid}", json={"external_session_id": "sid-old", "context_epoch": 0}
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "conflict"
    assert (await client.get(f"/v1/sessions/{sid}")).json()["external_session_id"] is None

    current = await client.patch(
        f"/v1/sessions/{sid}", json={"external_session_id": "sid-new", "context_epoch": 1}
    )
    assert current.status_code == 200, current.text
    assert current.json()["external_session_id"] == "sid-new"


async def test_codex_thread_id_patch_from_before_a_reset_is_refused(
    client: httpx.AsyncClient,
) -> None:
    from omnigent.runner.native.orchestration import _patch_codex_external_session_id

    sid, [reset], _ = await _reset_twice(client, 200, {})
    assert reset.status_code == 200, reset.text

    stale = await _patch_codex_external_session_id(
        client, session_id=sid, thread_id="thread-old", context_epoch=0
    )
    assert stale.status_code == 409, stale.text
    assert (await client.get(f"/v1/sessions/{sid}")).json()["external_session_id"] is None

    current = await _patch_codex_external_session_id(
        client, session_id=sid, thread_id="thread-new", context_epoch=1
    )
    assert current.status_code == 200, current.text
    assert (await client.get(f"/v1/sessions/{sid}")).json()["external_session_id"] == "thread-new"

"""Integration tests for ``POST /v1/sessions/{id}/reset`` (in-place context reset).

The server forwards a ``reset`` prepare to the bound runner, which drops its
cached history (200) or, on an older runner, ignores it (204). Only after the
runner agrees is the ``context_reset`` boundary persisted, then a ``commit``
drops anything cached meanwhile.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from omnigent.server.routes._sessions import reset as reset_module
from omnigent.server.routes._sessions.common import _session_status_cache
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


def _fake_runner(
    reset_status: int,
    summarize_status: int = 200,
    on_summarize: Callable[[], None] | None = None,
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """Build a mock runner answering ``reset`` and ``/v1/summarize``."""
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/summarize":
            if on_summarize is not None:
                on_summarize()
            return httpx.Response(summarize_status, json={"text": SUMMARY_TEXT, "token_count": 3})
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


SUMMARY_TEXT = "the generated summary"
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


async def test_reset_refuses_auto_summary_for_a_harness_without_replay(
    client: httpx.AsyncClient,
) -> None:
    sid, (resp,), captured = await _reset_twice(client, 200, {"summary": "auto"})

    assert resp.status_code == 400, resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


async def create_replay_session(
    client: httpx.AsyncClient, messages: Sequence[str] = ("hello",)
) -> str:
    """Create a session holding ``messages`` on a harness that replays a summary."""
    agent = await create_test_agent(
        client, executor={"type": "omnigent", "config": {"harness": "goose"}}
    )
    items = [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        }
        for text in messages
    ]
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": items}
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_auto_summary_is_carried_over_the_boundary(client: httpx.AsyncClient) -> None:
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner(200)
    set_runner_client(runner)
    try:
        sid = await create_replay_session(client)
        resp = await client.post(f"/v1/sessions/{sid}/reset", json={"summary": "auto"})
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 200, resp.text
    assert captured == [_PREPARE, _COMMIT]
    (boundary,) = await _reset_items(client, sid)
    assert boundary["summary"] == SUMMARY_TEXT


async def test_failed_auto_summary_leaves_the_conversation_unchanged(
    client: httpx.AsyncClient,
) -> None:
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner(200, summarize_status=500)
    set_runner_client(runner)
    try:
        sid = await create_replay_session(client)
        resp = await client.post(f"/v1/sessions/{sid}/reset", json={"summary": "auto"})
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code >= 400, resp.text
    assert "compaction.model" in resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


async def test_auto_summary_without_a_runner_never_uses_server_credentials(
    client: httpx.AsyncClient,
) -> None:
    sid = await create_replay_session(client)

    resp = await client.post(f"/v1/sessions/{sid}/reset", json={"summary": "auto"})

    assert resp.status_code == 503, resp.text
    assert "runner_unavailable" in resp.text
    assert await _reset_items(client, sid) == []


async def test_reset_of_a_running_session_is_refused(client: httpx.AsyncClient) -> None:
    sid, (resp,), captured = await _reset_twice(client, 200, {}, status="running")

    assert resp.status_code == 409, resp.text
    assert "session_busy" in resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        {"type": "slash_command", "data": {"kind": "skill", "name": "grill-me"}},
    ],
    ids=["message", "skill_command"],
)
async def test_turn_input_is_refused_while_a_reset_is_in_flight(
    client: httpx.AsyncClient, event: dict[str, Any]
) -> None:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": []}
    )
    sid = created.json()["id"]
    reset_module._reset_in_flight[sid] = reset_module._InFlightReset(stable_id=None)
    try:
        resp = await client.post(f"/v1/sessions/{sid}/events", json=event)
    finally:
        reset_module._reset_in_flight.pop(sid, None)

    assert resp.status_code == 409, resp.text
    assert "reset_in_progress" in resp.text


async def _run_auto_reset(
    client: httpx.AsyncClient, on_summarize: Callable[[str], None]
) -> tuple[str, httpx.Response, list[dict[str, Any]]]:
    """POST an auto-summary reset; ``on_summarize`` runs while the summary is written."""
    from omnigent.runtime import set_runner_client

    session: list[str] = []
    runner, captured = _fake_runner(200, on_summarize=lambda: on_summarize(session[0]))
    set_runner_client(runner)
    try:
        session.append(await create_replay_session(client))
        resp = await client.post(f"/v1/sessions/{session[0]}/reset", json={"summary": "auto"})
    finally:
        await runner.aclose()
        set_runner_client(None)
    return session[0], resp, captured


async def test_turn_started_during_the_summary_refuses_the_reset(
    client: httpx.AsyncClient,
) -> None:
    def start_turn(session_id: str) -> None:
        _session_status_cache[session_id] = "running"

    sid, resp, captured = await _run_auto_reset(client, start_turn)
    _session_status_cache.pop(sid, None)

    assert resp.status_code == 409, resp.text
    assert "session_busy" in resp.text
    assert captured == []
    assert await _reset_items(client, sid) == []


async def test_summariser_timeout_is_named_in_the_error(client: httpx.AsyncClient) -> None:
    def time_out(session_id: str) -> None:
        raise httpx.ReadTimeout("")

    sid, resp, _ = await _run_auto_reset(client, time_out)

    assert resp.status_code >= 400, resp.text
    assert "ReadTimeout" in resp.text
    assert await _reset_items(client, sid) == []


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
    from omnigent.harnesses.codex_native.forwarder import patch_codex_external_session_id

    sid, [reset], _ = await _reset_twice(client, 200, {})
    assert reset.status_code == 200, reset.text

    stale = await patch_codex_external_session_id(
        client, session_id=sid, thread_id="thread-old", context_epoch=0
    )
    assert stale.status_code == 409, stale.text
    assert (await client.get(f"/v1/sessions/{sid}")).json()["external_session_id"] is None

    current = await patch_codex_external_session_id(
        client, session_id=sid, thread_id="thread-new", context_epoch=1
    )
    assert current.status_code == 200, current.text
    assert (await client.get(f"/v1/sessions/{sid}")).json()["external_session_id"] == "thread-new"

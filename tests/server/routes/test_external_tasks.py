"""Tests for read-only external task rows (``/v1/external-tasks``)."""

from __future__ import annotations

import httpx

from omnigent.db.utils import builtin_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


def _seed_placeholder_agent(db_uri: str) -> None:
    """Seed the built-in agent because focused app tests skip lifespan startup."""
    SqlAlchemyAgentStore(db_uri).create(
        builtin_agent_id("claude-native-ui"),
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )


async def _list_row(client: httpx.AsyncClient, session_id: str) -> dict:
    page = (await client.get("/v1/sessions")).json()
    rows = [row for row in page["data"] if row["id"] == session_id]
    assert len(rows) == 1, page
    return rows[0]


async def test_external_task_lifecycle_is_listed_read_only_and_deletable(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    _seed_placeholder_agent(db_uri)
    payload = {"title": "fk-gate-7", "unit": "fk-gate-7", "host_id": None, "status": "running"}

    created = await client.post("/v1/external-tasks", json=payload)
    repeated = await client.post("/v1/external-tasks", json=payload)

    assert created.status_code == 201, created.text
    assert repeated.status_code == 200
    session_id = created.json()["id"]
    assert repeated.json()["id"] == session_id
    assert created.json()["status"] == "running"
    row = await _list_row(client, session_id)
    assert row["status"] == "running"
    assert row["labels"]["omnigent.external.unit"] == "fk-gate-7"

    updated = await client.patch(
        f"/v1/external-tasks/{session_id}",
        json={"status": "failed", "exit_code": 2, "detail": "gate red"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["exit_code"] == 2
    assert updated.json()["state"] == "failed"
    row = await _list_row(client, session_id)
    assert row["status"] == "failed"
    assert row["labels"]["omnigent.external.exit_code"] == "2"

    message = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "message", "data": {"role": "user", "content": "hi"}},
    )
    peer = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "session_message", "data": {"message": "hi"}},
    )
    assert message.status_code == 409, message.text
    assert peer.status_code == 409, peer.text

    deleted = await client.delete(f"/v1/sessions/{session_id}")
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(f"/v1/sessions/{session_id}")).status_code == 404


async def test_external_task_rejects_bad_input(client: httpx.AsyncClient, db_uri: str) -> None:
    _seed_placeholder_agent(db_uri)
    session_id = (
        await client.post("/v1/external-tasks", json={"title": "t", "unit": "u"})
    ).json()["id"]

    unknown_status = await client.patch(
        f"/v1/external-tasks/{session_id}", json={"status": "exploded"}
    )
    plain_text = await client.post(
        "/v1/external-tasks",
        content='{"title": "t", "unit": "u2"}',
        headers={"content-type": "text/plain"},
    )
    missing = await client.patch("/v1/external-tasks/nope", json={"status": "idle"})

    assert unknown_status.status_code == 400
    assert plain_text.status_code in (400, 415)
    assert missing.status_code == 404


async def test_external_task_patch_refuses_normal_sessions(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    _seed_placeholder_agent(db_uri)
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    normal = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="normal", agent_id=builtin_agent_id("claude-native-ui")
    )

    response = await client.patch(f"/v1/external-tasks/{normal.id}", json={"status": "idle"})

    assert response.status_code == 404


async def test_external_task_never_launches_a_runner() -> None:
    from types import SimpleNamespace

    import pytest

    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_impl

    row = SimpleNamespace(id="conv_x", labels={"omnigent.external.kind": "systemd_unit"})

    with pytest.raises(OmnigentError, match="no runner to launch"):
        await _launch_runner_on_host_impl(row, None, None, None)  # type: ignore[arg-type]

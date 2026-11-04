"""Routes that feed the routing outcome stream: accept, stop, delete, interrupt."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server import routing_outcomes as ro
from omnigent.server import shadow_routing as sr
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_EVENTS = "omnigent.server.routes.sessions.routes_events"


@pytest_asyncio.fixture()
async def routed_session(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A session with a logged routing decision ``d1`` and $2.50 of billed usage."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="a", bundle_location="test:///bundle")
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=agent_id)
    store.set_session_usage(conv.id, {"total_cost_usd": 2.5, "policy_cost_usd": 3.0})
    sr.append_decision({"decision_id": "d1", "session_id": conv.id, "backend": "rules"})
    return conv.id


def _events(event: str) -> list[dict]:
    return [r for r in ro.iter_outcomes() if r["event"] == event]


async def test_accept_records_for_routed_session(
    client: httpx.AsyncClient, routed_session: str
) -> None:
    resp = await client.post(f"/v1/sessions/{routed_session}/accept")
    assert resp.status_code == 200 and resp.json() == {"recorded": True}
    assert ro.summarize_outcomes()["d1"]["accept_at"] is not None


async def test_accept_without_decision_is_not_an_error(
    client: httpx.AsyncClient, routed_session: str, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = store.get_conversation(routed_session).agent_id  # type: ignore[union-attr]
    other = store.create_conversation(agent_id=agent_id)
    resp = await client.post(f"/v1/sessions/{other.id}/accept")
    assert resp.status_code == 200 and resp.json() == {"recorded": False}


async def test_delete_records_ended_with_final_usage(
    client: httpx.AsyncClient, routed_session: str
) -> None:
    resp = await client.delete(f"/v1/sessions/{routed_session}")
    assert resp.status_code == 200
    (ended,) = _events("ended")
    assert (ended["usd_total"], ended["usd_policy"], ended["reason"]) == (2.5, 3.0, "delete")


async def test_stop_records_ended(client: httpx.AsyncClient, routed_session: str) -> None:
    with patch(f"{_EVENTS}._stop_session_via_runner", AsyncMock(return_value=True)):
        resp = await client.post(
            f"/v1/sessions/{routed_session}/events", json={"type": "stop_session"}
        )
    assert resp.status_code == 202
    (ended,) = _events("ended")
    assert (ended["usd_total"], ended["reason"]) == (2.5, "stop")


@pytest.mark.parametrize("delivered", [True, False])
async def test_interrupt_is_an_override_only_when_it_lands(
    client: httpx.AsyncClient, routed_session: str, delivered: bool
) -> None:
    runner = SimpleNamespace(
        post=AsyncMock(return_value=SimpleNamespace(status_code=204 if delivered else 500))
    )
    with patch(f"{_EVENTS}._get_runner_client", AsyncMock(return_value=runner)):
        resp = await client.post(
            f"/v1/sessions/{routed_session}/events", json={"type": "interrupt"}
        )
    assert resp.status_code == 202
    overrides = ro.summarize_outcomes().get("d1", {}).get("overrides", [])
    assert [(o["kind"], o["actor"]) for o in overrides] == (
        [("interrupt", "owner")] if delivered else []
    )


async def test_runner_originated_interrupt_is_not_an_owner_override(
    client: httpx.AsyncClient, routed_session: str
) -> None:
    runner = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=204)))
    with patch(f"{_EVENTS}._get_runner_client", AsyncMock(return_value=runner)):
        for origin in ("agent", "system"):
            await client.post(
                f"/v1/sessions/{routed_session}/events",
                json={"type": "interrupt", "data": {"origin": origin}},
            )
    overrides = ro.summarize_outcomes()["d1"]["overrides"]
    assert [(o["kind"], o["actor"]) for o in overrides] == [
        ("interrupt", "agent"),
        ("interrupt", "system"),
    ]


async def test_refused_reset_leaves_no_interrupt_override(
    client: httpx.AsyncClient, routed_session: str
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError

    runner = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=204)))

    async def interrupt_then_refuse(**kwargs: object) -> None:
        await kwargs["interrupt"]()  # type: ignore[operator]
        raise OmnigentError("busy", code=ErrorCode.RESET_IN_PROGRESS)

    with (
        patch(f"{_EVENTS}._get_runner_client", AsyncMock(return_value=runner)),
        patch(f"{_EVENTS}.run_context_reset", AsyncMock(side_effect=interrupt_then_refuse)),
    ):
        await client.post(
            f"/v1/sessions/{routed_session}/events",
            json={"type": "reset", "data": {"summary": "none"}},
        )
    assert ro.summarize_outcomes().get("d1", {}).get("overrides", []) == []


async def test_accept_records_owner_actor_and_unknown_session_is_404(
    client: httpx.AsyncClient, routed_session: str
) -> None:
    await client.post(f"/v1/sessions/{routed_session}/accept")
    (accept,) = _events("accept")
    assert (accept["actor"], accept["session_id"]) == ("owner", routed_session)
    missing = await client.post("/v1/sessions/0123456789abcdef0123456789abcdef/accept")
    assert missing.status_code == 404
    assert len(_events("accept")) == 1


async def test_stop_snapshot_is_reread_after_the_runner_stops(
    client: httpx.AsyncClient, routed_session: str, db_uri: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    async def stop_and_flush_spend(*args: object, **kwargs: object) -> bool:
        store.set_session_usage(routed_session, {"total_cost_usd": 4.0})
        return True

    with patch(f"{_EVENTS}._stop_session_via_runner", stop_and_flush_spend):
        await client.post(f"/v1/sessions/{routed_session}/events", json={"type": "stop_session"})
    (ended,) = _events("ended")
    assert ended["usd_total"] == 4.0 and ended["native_cost_complete"] is None


@pytest.mark.parametrize("refused", [False, True])
async def test_reset_is_an_override_only_when_it_commits(
    client: httpx.AsyncClient, routed_session: str, refused: bool
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError

    outcome = (
        AsyncMock(side_effect=OmnigentError("busy", code=ErrorCode.RESET_IN_PROGRESS))
        if refused
        else AsyncMock(return_value=SimpleNamespace(epoch=1, boundary_item_id="item_1"))
    )
    with patch(f"{_EVENTS}.run_context_reset", outcome):
        await client.post(
            f"/v1/sessions/{routed_session}/events",
            json={"type": "reset", "data": {"summary": "none"}},
        )
    overrides = ro.summarize_outcomes().get("d1", {}).get("overrides", [])
    assert [o["kind"] for o in overrides] == ([] if refused else ["reset"])


@pytest.mark.parametrize(("previous", "expected"), [(None, []), ("opus", ["model_change"])])
async def test_terminal_model_switch_is_an_override_but_the_launch_report_is_not(
    client: httpx.AsyncClient,
    routed_session: str,
    db_uri: str,
    previous: str | None,
    expected: list[str],
) -> None:
    if previous is not None:
        SqlAlchemyConversationStore(db_uri).update_conversation(
            routed_session, reported_model=previous
        )
    await client.post(
        f"/v1/sessions/{routed_session}/events",
        json={"type": "external_model_change", "data": {"model": "claude-sonnet-4-6"}},
    )
    overrides = ro.summarize_outcomes().get("d1", {}).get("overrides", [])
    assert [o["kind"] for o in overrides] == expected


def _grant(db_uri: str, user: str, session_id: str, level: int) -> dict[str, str]:
    """Grant ``user`` ``level`` on the session; returns that user's identity header."""
    SqlAlchemyPermissionStore(db_uri).grant(f"{user}@example.com", session_id, level)
    return {"X-Forwarded-Email": f"{user}@example.com"}


@pytest.mark.parametrize(
    ("level", "allowed"),
    [(LEVEL_READ, False), (LEVEL_EDIT, False), (LEVEL_MANAGE, False), (LEVEL_OWNER, True)],
)
async def test_accept_is_owner_only(
    auth_client: httpx.AsyncClient, routed_session: str, db_uri: str, level: int, allowed: bool
) -> None:
    headers = _grant(db_uri, "caller", routed_session, level)
    resp = await auth_client.post(f"/v1/sessions/{routed_session}/accept", headers=headers)
    assert resp.status_code == (200 if allowed else 403)
    assert len(_events("accept")) == (1 if allowed else 0)


async def test_accept_by_a_user_without_a_grant_records_nothing(
    auth_client: httpx.AsyncClient, routed_session: str
) -> None:
    resp = await auth_client.post(
        f"/v1/sessions/{routed_session}/accept",
        headers={"X-Forwarded-Email": "stranger@example.com"},
    )
    assert resp.status_code in (403, 404)
    assert _events("accept") == []


@pytest.mark.parametrize(
    ("level", "actor"), [(LEVEL_EDIT, "collaborator"), (LEVEL_OWNER, "owner")]
)
async def test_interrupt_actor_follows_resolved_access(
    auth_client: httpx.AsyncClient, routed_session: str, db_uri: str, level: int, actor: str
) -> None:
    headers = _grant(db_uri, "caller", routed_session, level)
    runner = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=204)))
    with patch(f"{_EVENTS}._get_runner_client", AsyncMock(return_value=runner)):
        resp = await auth_client.post(
            f"/v1/sessions/{routed_session}/events", json={"type": "interrupt"}, headers=headers
        )
    assert resp.status_code == 202
    overrides = ro.summarize_outcomes()["d1"]["overrides"]
    assert [(o["kind"], o["actor"]) for o in overrides] == [("interrupt", actor)]


async def test_admin_interrupt_counts_as_owner(
    auth_client: httpx.AsyncClient, routed_session: str, db_uri: str
) -> None:
    store = SqlAlchemyPermissionStore(db_uri)
    store.ensure_user("admin@example.com")
    store.set_admin("admin@example.com", True)
    runner = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=204)))
    with patch(f"{_EVENTS}._get_runner_client", AsyncMock(return_value=runner)):
        resp = await auth_client.post(
            f"/v1/sessions/{routed_session}/events",
            json={"type": "interrupt"},
            headers={"X-Forwarded-Email": "admin@example.com"},
        )
    assert resp.status_code == 202
    overrides = ro.summarize_outcomes()["d1"]["overrides"]
    assert [(o["kind"], o["actor"]) for o in overrides] == [("interrupt", "owner")]

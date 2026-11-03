"""POST /v1/sessions with ``host_needs`` places the session through ag-fleet."""

from __future__ import annotations

import asyncio
import json

from omnigent.db.utils import now_epoch
from omnigent.server.host_placement import PLACEMENT_REASON_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.fake_ag_fleet import install_fake_ag_fleet, read_calls
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_permissions import (
    _create_session_as,
    _register_online_host,
)


async def _wait_for_calls(directory, count):
    for _ in range(100):
        if len(read_calls(directory)) >= count:
            break
        await asyncio.sleep(0.05)
    return read_calls(directory)


def _answer_host(auth_app, stat_result=None):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame

    frames = []

    def answer(connection, payload):
        frame = decode_host_frame(payload)
        frames.append((connection.host_id, frame))
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                stat_result
                or {
                    "status": "ok",
                    "exists": True,
                    "type": "directory",
                    "canonical_path": frame.path,
                }
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})

    auth_app.state.host_registry.send_text = answer
    return frames


async def test_create_with_host_needs_places_launches_and_releases(
    auth_client, auth_app, db_uri, tmp_path, monkeypatch
):
    host_id = "c" * 32
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.update_harness_readiness(
        host_id, {"claude-sdk": True, "codex": "needs-auth"}
    )
    frames = _answer_host(auth_app)
    install_fake_ag_fleet(tmp_path, monkeypatch, host_id=host_id, reason="over capacity")
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers["X-Forwarded-Email"] = "alice"

    created = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "workspace": "/srv/work",
            "host_needs": ["claude-sdk"],
            "host_preference": "box",
        },
    )
    assert created.status_code == 201, created.text
    row = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert (row.host_id, row.workspace) == (host_id, "/srv/work")
    assert row.labels[PLACEMENT_REASON_LABEL_KEY] == "over capacity"
    assert {host for host, _ in frames} == {host_id}

    place, release = await _wait_for_calls(tmp_path, 2)
    assert place["args"][place["args"].index("--host") + 1] == "box"
    (snapshot_row,) = json.loads(place["stdin"])
    assert snapshot_row["host_id"] == host_id
    assert snapshot_row["configured_harnesses"] == ["claude-sdk"]
    assert snapshot_row["running_sessions"] == 0
    assert release["args"] == ["place", "--release", "res_0123456789abcdef", "--json"]


async def test_create_with_host_needs_validation_and_refusal(
    auth_client, auth_app, tmp_path, monkeypatch
):
    host_id = "d" * 32
    _register_online_host(auth_app, host_id, "alice")
    install_fake_ag_fleet(tmp_path, monkeypatch, mode="refuse")
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers["X-Forwarded-Email"] = "alice"
    base = {"agent_id": source["agent_id"], "host_needs": ["claude-sdk"]}

    no_workspace = await auth_client.post("/v1/sessions", json=base)
    assert no_workspace.status_code == 400, no_workspace.text
    ambiguous = await auth_client.post(
        "/v1/sessions", json={**base, "workspace": "/w", "host_id": host_id}
    )
    assert ambiguous.status_code == 400, ambiguous.text
    assert read_calls(tmp_path) == []

    refused = await auth_client.post("/v1/sessions", json={**base, "workspace": "/w"})
    assert refused.status_code == 409, refused.text
    assert "quiet_window" in refused.text
    assert "optiplex is quiet 00:00-07:00 UTC" in refused.text
    assert len(read_calls(tmp_path)) == 1  # a refusal holds no reservation to release


async def test_snapshot_counts_sessions_with_a_live_runner(
    auth_client, auth_app, db_uri, tmp_path, monkeypatch
):
    host_id = "e" * 32
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.update_harness_readiness(host_id, {"claude-sdk": True})
    _answer_host(auth_app)
    install_fake_ag_fleet(tmp_path, monkeypatch, host_id=host_id)
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers["X-Forwarded-Email"] = "alice"
    body = {"agent_id": source["agent_id"], "workspace": "/w", "host_needs": ["claude-sdk"]}

    first = await auth_client.post("/v1/sessions", json=body)
    assert first.status_code == 201, first.text
    store = SqlAlchemyConversationStore(db_uri)
    runner_id = first.json()["runner_id"]
    store.touch_runner_liveness([runner_id], now_epoch())
    assert store.count_running_sessions_by_host([host_id]) == {host_id: 1}
    store.touch_runner_liveness([runner_id], now_epoch() - 10_000)
    assert store.count_running_sessions_by_host([host_id]) == {}
    store.touch_runner_liveness([runner_id], now_epoch())

    second = await auth_client.post("/v1/sessions", json=body)
    assert second.status_code == 201, second.text
    calls = await _wait_for_calls(tmp_path, 4)
    place_calls = [call for call in calls if "--release" not in call["args"]]
    assert json.loads(place_calls[1]["stdin"])[0]["running_sessions"] == 1


async def test_failed_launch_after_placement_still_releases(
    auth_client, auth_app, tmp_path, monkeypatch
):
    host_id = "f" * 32
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.update_harness_readiness(host_id, {"claude-sdk": True})
    _answer_host(auth_app, stat_result={"status": "ok", "exists": False})
    install_fake_ag_fleet(tmp_path, monkeypatch, host_id=host_id)
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers["X-Forwarded-Email"] = "alice"

    created = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "workspace": "/w", "host_needs": ["claude-sdk"]},
    )
    assert created.status_code >= 400, created.text
    calls = await _wait_for_calls(tmp_path, 2)
    assert calls[-1]["args"][:2] == ["place", "--release"]


async def test_caller_cannot_forge_placement_reason_label(
    auth_client, auth_app, db_uri, tmp_path, monkeypatch
):
    host_id = "9" * 32
    _register_online_host(auth_app, host_id, "alice")
    auth_app.state.host_store.update_harness_readiness(host_id, {"claude-sdk": True})
    _answer_host(auth_app)
    install_fake_ag_fleet(tmp_path, monkeypatch, host_id=host_id, reason="")
    source = await _create_session_as(auth_client, "", "alice")
    auth_client.headers["X-Forwarded-Email"] = "alice"
    created = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "workspace": "/w",
            "host_needs": ["claude-sdk"],
            "labels": {PLACEMENT_REASON_LABEL_KEY: "forged"},
        },
    )
    assert created.status_code == 201, created.text
    row = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert PLACEMENT_REASON_LABEL_KEY not in row.labels

"""Tests for the ag-fleet placement client."""

from __future__ import annotations

import json

import pytest

from omnigent.db.utils import now_epoch
from omnigent.server import host_placement
from omnigent.server.host_placement import (
    PlacementError,
    PlacementRefusedError,
    build_hosts_snapshot,
    place_host,
    release_reservation,
)
from omnigent.stores.host_store import Host
from tests.server.fake_ag_fleet import install_fake_ag_fleet, read_calls

HOST_ID = "a" * 32


async def _place(**overrides):
    kwargs = {"needs": ["claude-sdk"], "snapshot": [], "request_id": "req-1"}
    return await place_host(**{**kwargs, **overrides})


async def test_place_ok_parses_reply_and_passes_contract_args(tmp_path, monkeypatch):
    install_fake_ag_fleet(tmp_path, monkeypatch, host_id=HOST_ID)
    result = await _place(needs=["a", "b"], host="box", profile="gpu")
    assert result.host_id == HOST_ID
    assert (result.reservation_id, result.ttl_s) == ("res_0123456789abcdef", 120)
    assert result.candidates[0].eligible is True
    args = read_calls(tmp_path)[0]["args"]
    assert args == [
        "place", "--needs", "a", "--needs", "b", "--host", "box", "--profile", "gpu",
        "--ttl", "120", "--request-id", "req-1", "--hosts-json", "-", "--json",
    ]  # fmt: skip


async def test_refusal_carries_code_and_reason(tmp_path, monkeypatch):
    install_fake_ag_fleet(tmp_path, monkeypatch, mode="refuse")
    with pytest.raises(PlacementRefusedError) as raised:
        await _place()
    assert raised.value.code == "quiet_window"
    assert raised.value.reason == "optiplex is quiet 00:00-07:00 UTC"


async def test_usage_error_is_internal(tmp_path, monkeypatch, caplog):
    install_fake_ag_fleet(tmp_path, monkeypatch, mode="usage")
    with pytest.raises(PlacementError, match=r"internal error") as raised:
        await _place()
    assert raised.value.internal is True
    assert "omnigent bug" in caplog.text


@pytest.mark.parametrize("mode", ["badjson", "bad_schema"])
async def test_unparseable_reply_is_internal_error(tmp_path, monkeypatch, mode):
    install_fake_ag_fleet(tmp_path, monkeypatch, mode=mode)
    with pytest.raises(PlacementError) as raised:
        await _place()
    assert raised.value.internal is True


async def test_timeout_kills_process_and_reports(tmp_path, monkeypatch):
    install_fake_ag_fleet(tmp_path, monkeypatch, mode="hang")
    monkeypatch.setattr(host_placement, "PLACEMENT_TIMEOUT_S", 0.5)
    with pytest.raises(PlacementError, match="placement timed out"):
        await _place()


async def test_missing_binary_is_clear_error(monkeypatch):
    monkeypatch.setenv("OMNIGENT_AG_FLEET_BIN", "/nonexistent/ag-fleet")
    with pytest.raises(PlacementError, match="could not be run"):
        await _place()
    monkeypatch.setenv("OMNIGENT_AG_FLEET_BIN", "definitely-not-on-path-ag-fleet")
    with pytest.raises(PlacementError, match="not found"):
        await _place()


async def test_release_ok_and_failure_never_raises(tmp_path, monkeypatch):
    install_fake_ag_fleet(tmp_path, monkeypatch)
    assert await release_reservation("res_0123456789abcdef") is True
    release_args = read_calls(tmp_path)[0]["args"]
    assert release_args == ["place", "--release", "res_0123456789abcdef", "--json"]
    monkeypatch.setenv("OMNIGENT_AG_FLEET_BIN", "/nonexistent/ag-fleet")
    assert await release_reservation("res_0123456789abcdef") is False


def test_snapshot_flattens_available_harnesses_and_counts_running():
    host = Host(
        host_id=HOST_ID,
        name="box",
        user_id="alice",
        status="online",
        created_at=1,
        updated_at=now_epoch(),
        configured_harnesses={"claude-sdk": True, "codex": "needs-auth", "pi": False},
    )
    bare = Host(
        host_id="b" * 32, name="old", user_id="alice", status="offline", created_at=1, updated_at=1
    )
    rows = build_hosts_snapshot([host, bare], {HOST_ID: 3})
    assert rows == [
        {"host_id": HOST_ID, "name": "box", "status": "online",
         "configured_harnesses": ["claude-sdk"], "running_sessions": 3},
        {"host_id": "b" * 32, "name": "old", "status": "offline",
         "configured_harnesses": [], "running_sessions": 0},
    ]  # fmt: skip
    json.dumps(rows)


def test_snapshot_marks_stale_hosts_offline_and_skips_sandbox_hosts():
    stale = Host(
        host_id=HOST_ID,
        name="crashed",
        user_id="alice",
        status="online",
        created_at=1,
        updated_at=1,
    )
    managed = Host(
        host_id="b" * 32,
        name="sandbox",
        user_id="alice",
        status="online",
        created_at=1,
        updated_at=now_epoch(),
        sandbox_provider="docker",
    )
    rows = build_hosts_snapshot([stale, managed], {})
    assert [(row["name"], row["status"]) for row in rows] == [("crashed", "offline")]


async def test_malformed_ok_reply_releases_issued_reservation(tmp_path, monkeypatch):
    install_fake_ag_fleet(tmp_path, monkeypatch, mode="malformed")
    with pytest.raises(PlacementError, match="invalid reply"):
        await _place()
    release = read_calls(tmp_path)[-1]["args"]
    assert release == ["place", "--release", "res_0123456789abcdef", "--json"]


async def test_non_executable_binary_target_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_AG_FLEET_BIN", str(tmp_path))
    with pytest.raises(PlacementError, match="could not be run"):
        await _place()

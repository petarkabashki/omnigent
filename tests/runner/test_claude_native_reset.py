"""Claude-native reset in place: bridge selection, hint wipe, phase order, launch hold."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import record_launch_epoch
from omnigent.runner.native.claude_reset import (
    ClaudeNativeReset,
    claude_launch_bridge_id,
    local_resume_hint_epoch,
    wipe_claude_resume_state,
)


@pytest.mark.parametrize(
    ("labelled_bridge_id", "expected"),
    [(None, "conv_a"), ("conv_a", "conv_a"), ("conv_a-cleared", "conv_a-cleared")],
)
def test_launch_bridge_id_keeps_a_cleared_sessions_own_bridge(
    labelled_bridge_id: str | None, expected: str
) -> None:
    assert claude_launch_bridge_id("conv_a", labelled_bridge_id) == expected


def test_wipe_removes_the_resume_hint(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({"claude_session_id": "old"}))
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("x")

    wipe_claude_resume_state(tmp_path)
    # A second wipe on an already-clean bridge is a no-op.
    wipe_claude_resume_state(tmp_path)

    assert not (tmp_path / "state.json").exists()
    assert unrelated.exists()


@pytest.mark.parametrize(("stamped", "expected"), [(None, 0), ("2", 2), ("junk", 0)])
def test_local_resume_hint_epoch_reads_the_launch_stamp(
    tmp_path: Path, stamped: str | None, expected: int
) -> None:
    if stamped is not None:
        (tmp_path / "launch_epoch").write_text(stamped)

    assert local_resume_hint_epoch(tmp_path) == expected
    record_launch_epoch(tmp_path, 3)
    assert local_resume_hint_epoch(tmp_path) == 3


def test_preparing_a_bridge_dir_forgets_the_previous_launch_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = claude_native_bridge.prepare_bridge_dir("conv_a", workspace=tmp_path)
    record_launch_epoch(bridge_dir, 2)

    claude_native_bridge.prepare_bridge_dir("conv_a", workspace=tmp_path)

    assert claude_native_bridge.read_launch_epoch(bridge_dir) is None


def _recording_reset(
    tmp_path: Path, calls: list[str], *, hold_s: float = 30.0
) -> tuple[ClaudeNativeReset, asyncio.Lock]:
    """Build a reset whose steps record their order and the launch lock's state."""
    ensure_lock = asyncio.Lock()
    state_file = tmp_path / "state.json"

    async def stop_pane(session_id: str) -> None:
        calls.append(f"stop {session_id} locked={ensure_lock.locked()}")
        # A hook racing the stop re-records the old Claude id.
        state_file.write_text(json.dumps({"claude_session_id": "old"}))

    async def bridge_dir_for_session(session_id: str) -> Path:
        calls.append(f"wipe {session_id} hint={state_file.exists()}")
        return tmp_path

    async def relaunch(session_id: str) -> None:
        calls.append(
            f"relaunch {session_id} hint={state_file.exists()} locked={ensure_lock.locked()}"
        )

    reset = ClaudeNativeReset(
        stop_pane=stop_pane,
        bridge_dir_for_session=bridge_dir_for_session,
        relaunch=relaunch,
        ensure_lock_for_session=lambda _session_id: ensure_lock,
        hold_s=hold_s,
    )
    return reset, ensure_lock


@pytest.mark.asyncio
async def test_prepare_holds_launches_off_until_commit_relaunches(tmp_path: Path) -> None:
    calls: list[str] = []
    reset, ensure_lock = _recording_reset(tmp_path, calls)

    await reset.prepare("conv_a")
    assert ensure_lock.locked()
    (tmp_path / "state.json").write_text(json.dumps({"claude_session_id": "late"}))
    await reset.commit("conv_a")

    assert calls == [
        "stop conv_a locked=True",
        "wipe conv_a hint=True",
        "stop conv_a locked=True",
        "wipe conv_a hint=True",
        "relaunch conv_a hint=False locked=False",
    ]


@pytest.mark.asyncio
async def test_launch_attempted_between_phases_waits_for_commit(tmp_path: Path) -> None:
    calls: list[str] = []
    reset, ensure_lock = _recording_reset(tmp_path, calls)

    async def ensure() -> None:
        async with ensure_lock:
            calls.append(f"ensure hint={(tmp_path / 'state.json').exists()}")

    await reset.prepare("conv_a")
    waiting = asyncio.create_task(ensure())
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await reset.commit("conv_a")
    await waiting

    assert "ensure hint=False" in calls
    assert calls.index("ensure hint=False") > calls.index("stop conv_a locked=True", 1)


@pytest.mark.asyncio
async def test_uncommitted_prepare_releases_launches_after_the_hold(tmp_path: Path) -> None:
    calls: list[str] = []
    reset, ensure_lock = _recording_reset(tmp_path, calls, hold_s=0.01)

    await reset.prepare("conv_a")
    await asyncio.wait_for(ensure_lock.acquire(), timeout=1.0)
    ensure_lock.release()
    # A later reset is not blocked by the abandoned one.
    await asyncio.wait_for(reset.prepare("conv_a"), timeout=1.0)
    await reset.commit("conv_a")

    assert not ensure_lock.locked()
    assert calls[-1] == "relaunch conv_a hint=False locked=False"


@pytest.mark.asyncio
async def test_repeated_prepare_replaces_the_previous_hold(tmp_path: Path) -> None:
    calls: list[str] = []
    reset, ensure_lock = _recording_reset(tmp_path, calls)

    await reset.prepare("conv_a")
    await asyncio.wait_for(reset.prepare("conv_a"), timeout=1.0)
    await reset.commit("conv_a")

    assert not ensure_lock.locked()


@pytest.mark.asyncio
async def test_commit_without_prepare_stops_a_live_pane_before_relaunch(tmp_path: Path) -> None:
    calls: list[str] = []
    reset, ensure_lock = _recording_reset(tmp_path, calls)

    await reset.commit("conv_a")

    assert calls == [
        "stop conv_a locked=True",
        "wipe conv_a hint=True",
        "relaunch conv_a hint=False locked=False",
    ]
    assert not ensure_lock.locked()

"""Codex-native reset in place: forwarder cursor wipe, phase order, launch hold."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import read_launch_epoch, record_launch_epoch
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    read_bridge_state,
    read_child_usage_baselines,
    record_child_usage_baseline,
    write_bridge_state,
)
from omnigent.harnesses.codex_native.forwarder import (
    _replay_dead_letters_before_resume,
    reset_codex_forward_state,
)
from omnigent.native._native_post_delivery import append_dead_letter
from omnigent.runner import turn_routing
from omnigent.runner.native import claude_reset, orchestration
from omnigent.runner.native.claude_reset import ClaudeNativeReset, ClaudeResetBusyError
from omnigent.runner.native.codex_reset import stamp_codex_launch_epoch, wipe_codex_resume_state


def _write_cursor(bridge_dir: Path) -> None:
    """Leave the state a forwarder mid-turn with a pending shell command would resume from."""
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_a",
            socket_path="ws://127.0.0.1:1",
            thread_id="thread_old",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id="turn_old",
            pending_shell_command_id="shell_old",
        ),
    )
    record_child_usage_baseline(bridge_dir, "thread_child", (5000, 4000, 100))


def test_forwarder_reset_drops_the_replay_cursor_and_child_baselines(tmp_path: Path) -> None:
    _write_cursor(tmp_path)
    assert read_bridge_state(tmp_path) is not None

    reset_codex_forward_state(tmp_path)
    # A second reset on an already-clean bridge is a no-op.
    reset_codex_forward_state(tmp_path)

    assert read_bridge_state(tmp_path) is None
    assert read_child_usage_baselines(tmp_path) == {}


def test_wipe_also_drops_a_pending_routed_prompt_and_keeps_unrelated_files(
    tmp_path: Path,
) -> None:
    _write_cursor(tmp_path)
    turn_routing.write_pending_replay(
        tmp_path, session_id="conv_a", prompt="before the boundary", blocked_turn_id="turn_old"
    )
    unrelated = tmp_path / "bridge.json"
    unrelated.write_text("{}")

    wipe_codex_resume_state(tmp_path)

    assert read_bridge_state(tmp_path) is None
    assert turn_routing.read_pending_replay(tmp_path) is None
    assert unrelated.exists()


@pytest.mark.parametrize(
    ("stamped_epoch", "thread_id", "wiped"),
    [
        pytest.param(1, None, True, id="reset_no_runner_ran"),
        pytest.param(None, None, True, id="never_stamped"),
        pytest.param(2, None, False, id="already_on_this_epoch"),
        pytest.param(1, "thread_new", False, id="resuming_a_thread_from_this_epoch"),
    ],
)
def test_a_launch_catches_up_on_a_reset_no_runner_ran(
    tmp_path: Path, stamped_epoch: int | None, thread_id: str | None, wiped: bool
) -> None:
    _write_cursor(tmp_path)
    if stamped_epoch is not None:
        record_launch_epoch(tmp_path, stamped_epoch)

    stamp_codex_launch_epoch(tmp_path, 2, thread_id=thread_id)

    assert read_launch_epoch(tmp_path) == 2
    assert (read_bridge_state(tmp_path) is None) is wiped
    assert (read_child_usage_baselines(tmp_path) == {}) is wiped


@pytest.mark.asyncio
async def test_a_runner_launch_catches_up_on_a_reset_no_runner_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path)
    bridge_dir = bridge_dir_for_bridge_id("conv_a")
    bridge_dir.mkdir()
    _write_cursor(bridge_dir)
    record_launch_epoch(bridge_dir, 1)

    class _StampDone(Exception):
        pass

    async def launch_config(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            external_session_id=None, workspace=tmp_path, bridge_id="conv_a", context_epoch=2
        )

    def stop_after_the_stamp(_bridge_dir: Path) -> Path:
        raise _StampDone

    monkeypatch.setattr(orchestration, "_codex_native_launch_config", launch_config)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge.socket_path_for_bridge_dir", stop_after_the_stamp
    )

    with pytest.raises(_StampDone):
        await orchestration._auto_create_codex_terminal(
            "conv_a", MagicMock(), lambda _session_id, _event: None
        )

    assert read_launch_epoch(bridge_dir) == 2
    assert read_bridge_state(bridge_dir) is None
    assert read_child_usage_baselines(bridge_dir) == {}


@pytest.mark.asyncio
async def test_a_dead_letter_from_before_a_reset_does_not_replay_after_the_boundary(
    tmp_path: Path,
) -> None:
    def dead_letter(source_id: str) -> None:
        append_dead_letter(
            tmp_path,
            session_id="conv_a",
            event_type="external_conversation_item",
            payload={"item_type": "message", "source_id": source_id},
            reason="transient HTTP failure after retries",
            http_status=503,
        )

    dead_letter("item_rotated_out")
    (tmp_path / "dead_letter.jsonl").replace(tmp_path / "dead_letter.jsonl.1")
    dead_letter("item_old")
    reposted: list[bytes] = []

    def repost(request: httpx.Request) -> httpx.Response:
        reposted.append(request.content)
        return httpx.Response(202, json={"queued": False})

    wipe_codex_resume_state(tmp_path)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(repost)
    ) as client:
        await _replay_dead_letters_before_resume(client, tmp_path)

    assert reposted == []
    assert list(tmp_path.glob("dead_letter*")) == []


def _codex_reset(
    tmp_path: Path, calls: list[str], *, hold_s: float = 30.0
) -> tuple[ClaudeNativeReset, asyncio.Lock]:
    """Build the shared reset phases configured as codex, recording step order."""
    ensure_lock = asyncio.Lock()

    async def stop_pane(session_id: str) -> None:
        calls.append(f"stop {session_id} locked={ensure_lock.locked()}")
        # A forwarder racing the stop re-records the old thread.
        _write_cursor(tmp_path)

    async def bridge_dir_for_session(session_id: str) -> Path:
        calls.append(f"wipe {session_id} cursor={read_bridge_state(tmp_path) is not None}")
        return tmp_path

    async def relaunch(session_id: str) -> None:
        calls.append(
            f"relaunch {session_id} cursor={read_bridge_state(tmp_path) is not None} "
            f"locked={ensure_lock.locked()}"
        )

    reset = ClaudeNativeReset(
        stop_pane=stop_pane,
        bridge_dir_for_session=bridge_dir_for_session,
        relaunch=relaunch,
        ensure_lock_for_session=lambda _session_id: ensure_lock,
        hold_s=hold_s,
        wipe=wipe_codex_resume_state,
        runtime_label="Codex",
    )
    return reset, ensure_lock


@pytest.mark.asyncio
async def test_prepare_holds_launches_off_until_commit_relaunches_without_the_cursor(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    reset, ensure_lock = _codex_reset(tmp_path, calls)

    await reset.prepare("conv_a")
    assert ensure_lock.locked()
    await reset.commit("conv_a")

    assert calls == [
        "stop conv_a locked=True",
        "wipe conv_a cursor=True",
        "stop conv_a locked=True",
        "wipe conv_a cursor=True",
        "relaunch conv_a cursor=False locked=False",
    ]


@pytest.mark.asyncio
async def test_prepare_refusal_names_codex_when_a_launch_outlasts_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_reset, "RESET_PREPARE_LOCK_WAIT_S", 0.05)
    calls: list[str] = []
    reset, ensure_lock = _codex_reset(tmp_path, calls)
    await ensure_lock.acquire()

    with pytest.raises(ClaudeResetBusyError, match=r"^Codex is still starting"):
        await reset.prepare("conv_a")

    assert calls == []

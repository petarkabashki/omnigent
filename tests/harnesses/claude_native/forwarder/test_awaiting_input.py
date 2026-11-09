"""Claude ``Notification`` hooks forward as awaiting-input events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.bridge import record_hook_event
from omnigent.harnesses.claude_native.forwarder import forward_claude_transcript_to_session
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


async def _forward_and_collect_awaiting_input(
    tmp_path: Path, notifications: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record *notifications* after a SessionStart and return the first awaiting-input body."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    for notification in notifications:
        record_hook_event(
            bridge_dir,
            {"hook_event_name": "Notification", "session_id": "claude-session", **notification},
        )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        while True:
            req = await asyncio.wait_for(_get_recorded_request(server), timeout=10)
            if req["body"].get("type") == "external_session_awaiting_input":
                return req["body"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("notification", "expected"),
    [
        (
            {"notification_type": "permission_prompt", "message": "Claude needs permission: Bash"},
            "Claude needs permission: Bash",
        ),
        ({"notification_type": "idle_prompt"}, "Claude is waiting for your input"),
    ],
)
async def test_notification_hook_posts_awaiting_input(
    tmp_path: Path, notification: dict[str, Any], expected: str
) -> None:
    body = await _forward_and_collect_awaiting_input(tmp_path, [notification])
    assert body["data"] == {"message": expected}


@pytest.mark.asyncio
async def test_non_blocking_notification_is_not_forwarded(tmp_path: Path) -> None:
    ignored = {"notification_type": "auth_success", "message": "Signed in"}
    followed_by = {"notification_type": "idle_prompt", "message": "Waiting"}
    body = await _forward_and_collect_awaiting_input(tmp_path, [ignored, followed_by])
    assert body["data"] == {"message": "Waiting"}

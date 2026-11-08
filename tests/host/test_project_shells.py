"""Tests for host-owned project shells (omnigent.host.project_shells)."""

from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

import pytest

from omnigent.host import runner_scope
from omnigent.host.frames import (
    HostProjectShellFrame,
    HostProjectShellResultFrame,
    HostShellWsFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.project_shells import ProjectShellError, ProjectShellManager
from omnigent.runner.transports.ws_tunnel.frames import (
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    decode_frame,
    encode_frame,
)

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def test_project_shell_frames_round_trip() -> None:
    request = HostProjectShellFrame(
        request_id="r1", op="create", project_id="p1", workspace="/w", title="T"
    )
    assert decode_host_frame(encode_host_frame(request)) == request
    result = HostProjectShellResultFrame(
        request_id="r1", status="ok", shells=[{"id": "pshell_1", "alive": True}]
    )
    assert decode_host_frame(encode_host_frame(result)) == result
    envelope = HostShellWsFrame(
        payload=encode_frame(WSCloseFrame(ch_id="c", code=1000, reason=""))
    )
    assert decode_host_frame(encode_host_frame(envelope)) == envelope


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ProjectShellManager:
    monkeypatch.setattr(runner_scope, "scope_supported", lambda: False)
    return ProjectShellManager(scope_limits=lambda: (None, None, None))


async def test_create_rejects_missing_workspace(manager: ProjectShellManager) -> None:
    with pytest.raises(ProjectShellError, match="does not exist"):
        await manager.create("p1", "/nonexistent/workspace")


@needs_tmux
async def test_create_list_close_lifecycle(manager: ProjectShellManager, tmp_path: Path) -> None:
    shell = await manager.create("p1", str(tmp_path))
    other = await manager.create("p2", str(tmp_path))
    try:
        assert shell["alive"] is True
        assert [s["id"] for s in await manager.list("p1")] == [shell["id"]]
        assert {s["id"] for s in await manager.list()} == {shell["id"], other["id"]}
        # Another project's id cannot close the shell.
        assert await manager.close(str(shell["id"]), "p2") is False
        assert await manager.close(str(shell["id"]), "p1") is True
        assert await manager.close(str(shell["id"]), "p1") is False
        assert [s["id"] for s in await manager.list()] == [other["id"]]
    finally:
        await manager.close_all()
    assert await manager.list() == []


@needs_tmux
async def test_scope_placement_uses_default_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    placed: list[dict[str, object]] = []
    scope = runner_scope.RunnerScope(unit="u.scope", cgroup_dir=tmp_path)
    monkeypatch.setattr(runner_scope, "scope_supported", lambda: True)
    monkeypatch.setattr(
        runner_scope, "place_runner_in_scope", lambda pid, **kw: placed.append(kw) or scope
    )
    stopped: list[object] = []
    monkeypatch.setattr(runner_scope, "stop_scope", lambda s: stopped.append(s) or True)
    manager = ProjectShellManager(scope_limits=lambda: (1 << 30, 1 << 29, 50))
    shell = await manager.create("p1", str(tmp_path))
    assert placed[0]["memory_max"] == 1 << 30
    assert placed[0]["memory_high"] == 1 << 29
    assert placed[0]["cpu_quota_percent"] == 50
    assert str(placed[0]["runner_id"]).startswith("shell-pshell_")
    await manager.close(str(shell["id"]))
    assert stopped == [scope]


@needs_tmux
async def test_attach_relays_input_and_output(
    manager: ProjectShellManager, tmp_path: Path
) -> None:
    shell = await manager.create("p1", str(tmp_path))
    sent: asyncio.Queue[str] = asyncio.Queue()

    async def send_text(text: str) -> None:
        sent.put_nowait(text)

    async def relay(frame: object) -> None:
        await manager.handle_ws_envelope(
            HostShellWsFrame(payload=encode_frame(frame)),  # type: ignore[arg-type]
            send_text,
        )

    try:
        path = f"/shells/{shell['id']}/attach"
        await relay(WSOpenFrame(ch_id="c1", path=path, query_string="read_only=false"))
        await relay(
            WSFrame(
                ch_id="c1",
                data=base64.b64encode(b"echo project-$((40+2))\r").decode(),
                encoding="base64",
            )
        )
        seen = b""
        async with asyncio.timeout(15):
            while b"project-42" not in seen.replace(b"\r", b""):
                envelope = decode_host_frame(await sent.get())
                assert isinstance(envelope, HostShellWsFrame)
                inner = decode_frame(envelope.payload)
                if isinstance(inner, WSFrame) and inner.encoding == "base64":
                    seen += base64.b64decode(inner.data)
        await relay(WSCloseFrame(ch_id="c1", code=1000, reason=""))
    finally:
        await manager.close_all()


@needs_tmux
async def test_attach_to_unknown_shell_closes_with_not_found(manager: ProjectShellManager) -> None:
    sent: asyncio.Queue[str] = asyncio.Queue()

    async def send_text(text: str) -> None:
        sent.put_nowait(text)

    await manager.handle_ws_envelope(
        HostShellWsFrame(
            payload=encode_frame(
                WSOpenFrame(ch_id="c1", path="/shells/nope/attach", query_string="")
            )
        ),
        send_text,
    )
    async with asyncio.timeout(5):
        while True:
            envelope = decode_host_frame(await sent.get())
            assert isinstance(envelope, HostShellWsFrame)
            inner = decode_frame(envelope.payload)
            if isinstance(inner, WSCloseFrame):
                assert inner.code == 4404
                break

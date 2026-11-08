"""Project shells: tmux shells owned by the host process, not by a session.

A project shell runs on the project's default host in the project's default
workspace and outlives every session. The host is the source of truth: the
server asks it to create, list and close shells (``host.project_shell`` frames)
and relays browser attaches through ``host.shell_ws`` envelopes that carry the
same ``ws.open`` / ``ws.frame`` / ``ws.close`` frames a runner tunnel uses.

Shells live in this process's memory; a host restart drops them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Query, WebSocket

from omnigent.host import runner_scope
from omnigent.host.frames import HostShellWsFrame, encode_host_frame
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame, _RunnerWSChannel
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.registry import TerminalRegistry
from omnigent.terminals.ws_common import WS_CLOSE_TERMINAL_NOT_FOUND
from omnigent.util.json_types import JsonObject

_logger = logging.getLogger(__name__)

_TERMINAL_NAME = "shell"
ScopeLimits = tuple[int | None, int | None, int | None]


class ProjectShellError(Exception):
    """A project-shell request that cannot be served; the message is user-facing."""


@dataclass
class _ProjectShell:
    shell_id: str
    project_id: str
    title: str
    created_at: int
    scope: runner_scope.RunnerScope | None = None


def conversation_key(project_id: str) -> str:
    """Registry key grouping a project's shells, e.g. ``"project:abc"``."""
    return f"project:{project_id}"


class ProjectShellManager:
    """Creates, lists, closes and attaches host-owned project shells."""

    def __init__(self, *, scope_limits: Callable[[], ScopeLimits]) -> None:
        """
        :param scope_limits: Returns ``(memory_max, memory_high, cpu_quota)`` for a new
            shell's scope: the host's default session limits.
        """
        self._scope_limits = scope_limits
        self._registry = TerminalRegistry()
        self._shells: dict[str, _ProjectShell] = {}
        self._ws_channels: dict[str, _RunnerWSChannel] = {}
        self.app = self._build_app()

    async def create(
        self, project_id: str, workspace: str, title: str | None = None
    ) -> JsonObject:
        """Start a shell in ``workspace`` inside its own memory-capped scope.

        :raises ProjectShellError: When the workspace is missing or the shell won't start.
        """
        if not workspace or not os.path.isdir(os.path.expanduser(workspace)):
            raise ProjectShellError(f"workspace path does not exist on this host: {workspace}")
        shell_id = f"pshell_{secrets.token_hex(4)}"
        cwd = os.path.expanduser(workspace)
        spec = TerminalEnvSpec(
            command=os.environ.get("SHELL") or "bash", os_env=OSEnvSpec(cwd=cwd)
        )
        try:
            instance = await self._registry.launch(
                conversation_key(project_id),
                _TERMINAL_NAME,
                shell_id,
                spec,
            )
        except Exception as exc:
            raise ProjectShellError(f"could not start the shell: {exc}") from exc
        shell = _ProjectShell(
            shell_id=shell_id,
            project_id=project_id,
            title=title
            or f"Shell {sum(s.project_id == project_id for s in self._shells.values()) + 1}",
            created_at=int(time.time()),
        )
        self._shells[shell_id] = shell
        shell.scope = await asyncio.to_thread(
            self._place_in_scope, instance.pane_pid_sync, shell_id
        )
        return await self._describe(shell)

    def _place_in_scope(self, pane_pid: Callable[[], int | None], shell_id: str):
        """Move the shell's process tree into its own scope; ``None`` when unsupported."""
        if not runner_scope.scope_supported():
            return None
        pid = pane_pid()
        if pid is None:
            return None
        memory_max, memory_high, cpu_quota = self._scope_limits()
        return runner_scope.place_runner_in_scope(
            pid,
            runner_id=f"shell-{shell_id}",
            memory_max=memory_max,
            memory_high=memory_high,
            cpu_quota_percent=cpu_quota,
        )

    async def list(self, project_id: str | None = None) -> list[JsonObject]:
        """Shells of ``project_id``, or of every project when ``None``."""
        shells = [s for s in self._shells.values() if project_id in (None, s.project_id)]
        return [await self._describe(shell) for shell in shells]

    async def close(self, shell_id: str, project_id: str | None = None) -> bool:
        """Kill a shell and its scope. ``False`` when it is unknown or another project's."""
        shell = self._shells.get(shell_id)
        if shell is None or project_id not in (None, shell.project_id):
            return False
        del self._shells[shell_id]
        await self._registry.close(conversation_key(shell.project_id), _TERMINAL_NAME, shell_id)
        if shell.scope is not None:
            await asyncio.to_thread(runner_scope.stop_scope, shell.scope)
        return True

    async def close_all(self) -> None:
        """Close every shell (host shutdown)."""
        for shell_id in list(self._shells):
            await self.close(shell_id)
        await self.drop_attaches()

    async def drop_attaches(self) -> None:
        """Cancel every relayed attach; the tunnel that carried them is gone."""
        tasks = [c.task for c in self._ws_channels.values() if c.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _describe(self, shell: _ProjectShell) -> JsonObject:
        instance = self._registry.get(
            conversation_key(shell.project_id), _TERMINAL_NAME, shell.shell_id
        )
        alive = instance is not None and instance.running and await instance.is_alive()
        return {
            "id": shell.shell_id,
            "project_id": shell.project_id,
            "title": shell.title,
            "created_at": shell.created_at,
            "alive": alive,
        }

    async def handle_ws_envelope(
        self, frame: HostShellWsFrame, send_text: Callable[[str], Awaitable[None]]
    ) -> None:
        """Feed one relayed attach frame to the shell ASGI app.

        Never blocks on the shell, so awaiting it inline keeps keystrokes in order.

        :param send_text: Sends an encoded host frame to the server.
        """

        async def send_payload(payload: str) -> None:
            await send_text(encode_host_frame(HostShellWsFrame(payload=payload)))

        await _handle_tunnel_frame(self.app, frame.payload, send_payload, {}, self._ws_channels)

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.websocket("/shells/{shell_id}/attach")
        async def attach(
            websocket: WebSocket, shell_id: str, read_only: bool = Query(default=False)
        ) -> None:
            await websocket.accept()
            shell = self._shells.get(shell_id)
            instance = (
                self._registry.get(conversation_key(shell.project_id), _TERMINAL_NAME, shell_id)
                if shell is not None
                else None
            )
            if instance is None or not instance.running or not await instance.is_alive():
                await websocket.close(
                    code=WS_CLOSE_TERMINAL_NOT_FOUND, reason="shell not found or not running"
                )
                return
            await bridge_tmux_control_to_websocket(
                websocket,
                socket_path=str(instance.socket_path),
                tmux_target=instance.tmux_target,
                read_only=read_only,
            )

        return app

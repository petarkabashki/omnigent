"""Server side of host-owned project shells.

The host owns the shells (see :mod:`omnigent.host.project_shells`); the server
asks it to create, list and close them and relays browser attaches. The relay
reuses the runner-tunnel WS channel: :class:`_TunneledWSConn` runs unchanged
over :class:`_HostShellChannels`, which wraps each frame in a ``host.shell_ws``
envelope on the host tunnel.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from omnigent.host.frames import (
    CAP_PROJECT_SHELLS,
    HostProjectShellFrame,
    HostProjectShellResultFrame,
    HostShellWsFrame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import WSCloseFrame, WSFrame, decode_frame
from omnigent.runner.transports.ws_tunnel.registry import WSChannelState, ws_inbound_item
from omnigent.server._runner_ws_tunnel import _TunneledWSConn
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 30.0


def require_project_shell_support(conn: HostConnection) -> None:
    """Reject fast when the host predates project shells.

    :raises HTTPException: 409 with an update hint.
    """
    if CAP_PROJECT_SHELLS not in conn.hello.capabilities:
        raise HTTPException(
            status_code=409,
            detail=(
                f"host '{conn.hello.name}' runs omnigent {conn.hello.version}, which does not "
                "support project shells — update omnigent on the host and retry"
            ),
        )


async def request_project_shell(
    host_registry: HostRegistry, conn: HostConnection, **fields: Any
) -> HostProjectShellResultFrame:
    """Send a ``host.project_shell`` request and wait for its result.

    :param fields: :class:`HostProjectShellFrame` fields besides ``request_id``.
    :raises HTTPException: 502 when the connection drops, 504 on timeout.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    conn.pending_project_shells[request_id] = future
    try:
        try:
            host_registry.send_text(
                conn, encode_host_frame(HostProjectShellFrame(request_id=request_id, **fields))
            )
        except ConnectionError as exc:
            raise HTTPException(
                status_code=502, detail=f"host '{conn.host_id}' connection lost"
            ) from exc
        try:
            result = await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT_S)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=f"host '{conn.host_id}' did not answer within {_REQUEST_TIMEOUT_S:.0f}s",
            ) from exc
        return HostProjectShellResultFrame(request_id=request_id, **result)
    finally:
        conn.pending_project_shells.pop(request_id, None)


def resolve_project_shell_result(conn: HostConnection, frame: HostProjectShellResultFrame) -> None:
    """Complete the pending request a result frame answers."""
    future = conn.pending_project_shells.pop(frame.request_id, None)
    if future is not None and not future.done():
        future.set_result({"status": frame.status, "shells": frame.shells, "error": frame.error})


class _HostShellChannels:
    """The runner-registry calls :class:`_TunneledWSConn` makes, served over a host tunnel."""

    def __init__(self, host_registry: HostRegistry, conn: HostConnection) -> None:
        self._host_registry = host_registry
        self._conn = conn

    def open_ws_channel(
        self,
        runner_id: str,  # noqa: ARG002 - runner-registry signature
        ch_id: str,
        *,
        session: object = None,  # noqa: ARG002
    ) -> WSChannelState:
        state = WSChannelState(loop=asyncio.get_running_loop(), session=self._conn)  # type: ignore[arg-type]
        self._conn.shell_ws_channels[ch_id] = state
        return state

    def close_ws_channel(
        self,
        runner_id: str,  # noqa: ARG002 - runner-registry signature
        ch_id: str,
        session: object = None,  # noqa: ARG002
    ) -> None:
        self._conn.shell_ws_channels.pop(ch_id, None)

    async def send_text(self, session: object, data: str) -> None:  # noqa: ARG002
        self._host_registry.send_text(self._conn, encode_host_frame(HostShellWsFrame(data)))


def open_shell_attach(
    host_registry: HostRegistry, conn: HostConnection, shell_id: str, *, read_only: bool = False
) -> _TunneledWSConn:
    """A tunneled channel to a host-owned shell, shaped like a runner terminal attach."""
    return _TunneledWSConn(
        registry=_HostShellChannels(host_registry, conn),  # type: ignore[arg-type]
        session=SimpleNamespace(runner_id=conn.host_id),  # type: ignore[arg-type]
        runner_path=f"/shells/{shell_id}/attach?read_only={'true' if read_only else 'false'}",
    )


def deliver_shell_ws(conn: HostConnection, frame: HostShellWsFrame) -> None:
    """Queue a relayed host frame on its attach channel; orphans are dropped."""
    try:
        inner = decode_frame(frame.payload)
    except ValueError:
        return
    if not isinstance(inner, WSFrame | WSCloseFrame):
        return
    channel = conn.shell_ws_channels.get(inner.ch_id)
    item = ws_inbound_item(inner)
    if channel is not None and item is not None:
        channel.loop.call_soon_threadsafe(channel.inbound_queue.put_nowait, item)


def abort_shell_attaches(conn: HostConnection) -> None:
    """Tell every open attach its tunnel is gone."""
    for channel in list(conn.shell_ws_channels.values()):
        channel.loop.call_soon_threadsafe(channel.inbound_queue.put_nowait, None)
    conn.shell_ws_channels.clear()


async def close_shells(
    host_registry: HostRegistry,
    conn: HostConnection,
    shells: list[dict[str, Any]],
) -> None:
    """Close each shell, logging (not raising) failures."""
    for shell in shells:
        try:
            await request_project_shell(
                host_registry,
                conn,
                op="close",
                project_id=str(shell.get("project_id")),
                shell_id=str(shell.get("id")),
            )
        except HTTPException as exc:
            _logger.warning("closing project shell %s failed: %s", shell.get("id"), exc.detail)


async def reconcile_project_shells(
    host_registry: HostRegistry, conn: HostConnection, project_exists: Any
) -> None:
    """Close shells the host reports whose project no longer exists.

    :param project_exists: Callable ``(project_id) -> bool``, run in a thread.
    """
    if CAP_PROJECT_SHELLS not in conn.hello.capabilities:
        return
    try:
        listed = await request_project_shell(host_registry, conn, op="list")
        orphaned = [
            shell
            for shell in listed.shells
            if not await asyncio.to_thread(project_exists, str(shell.get("project_id")))
        ]
        await close_shells(host_registry, conn, orphaned)
    except HTTPException as exc:
        _logger.warning("project shell reconcile on %s failed: %s", conn.host_id, exc.detail)

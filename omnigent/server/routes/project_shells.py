"""REST and WebSocket routes for project shells (``/v1/projects/{id}/shells``).

A project shell is a tmux shell owned by the project's default host, started in
the project's default workspace. It outlives every session and is killed when the
project is deleted. Owner-only, like the projects router.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketException
from starlette import status

from omnigent.debug_logging import set_current_user_id
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_PROJECT_SHELLS
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.project_shells import (
    close_shells,
    open_shell_attach,
    request_project_shell,
    require_project_shell_support,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes.terminal_attach import relay_terminal_ws
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store import ProjectStore

logger = logging.getLogger(__name__)


async def connected_project_host(
    app_state: Any, project: Project, user_id: str | None
) -> tuple[HostRegistry, HostConnection, str]:
    """Resolve a project's default host connection and workspace.

    :returns: ``(host_registry, connection, workspace)``.
    :raises OmnigentError: 400 without a default host/workspace, 404 for an unknown
        host, 403 for another user's host, 503 when the host is offline.
    """
    host_id = project.config.get("host_id")
    workspace = project.config.get("workspace")
    if not isinstance(host_id, str) or not host_id.strip():
        raise OmnigentError(
            "This project has no default host. Set one in the project settings first.",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(workspace, str) or not workspace.strip():
        raise OmnigentError(
            "This project has no default workspace. Set one in the project settings first.",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry: HostRegistry = app_state.host_registry
    host_store: HostStore = app_state.host_store
    host = await asyncio.to_thread(host_store.get_host, host_id)
    if host is None:
        raise OmnigentError(
            "The project's default host no longer exists.", code=ErrorCode.NOT_FOUND
        )
    if user_id is not None and host.user_id != user_id:
        raise OmnigentError("The project's default host is not yours.", code=ErrorCode.FORBIDDEN)
    conn = host_registry.get(host.host_id)
    if conn is None:
        raise OmnigentError(
            f"The project's default host '{host.name}' is offline.", code=ErrorCode.HOST_OFFLINE
        )
    return host_registry, conn, workspace.strip()


async def close_deleted_project_shells(
    app_state: Any, project: Project | None, user_id: str | None
) -> None:
    """Kill a deleted project's shells on its host, when that host is reachable.

    Best effort: shells left on an offline or older host are closed by the
    reconcile that runs when the host reconnects.
    """
    if project is None:
        return
    try:
        host_registry, conn, _ = await connected_project_host(app_state, project, user_id)
        if CAP_PROJECT_SHELLS not in conn.hello.capabilities:
            return
        listed = await request_project_shell(host_registry, conn, op="list", project_id=project.id)
        await close_shells(host_registry, conn, listed.shells)
    except (OmnigentError, HTTPException):
        logger.info("project %s shells not closed on delete; reconcile will", project.id)


def create_project_shells_router(
    project_store: ProjectStore, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the project-shells router; mounted under ``/v1``."""
    router = APIRouter()

    async def owned_project(request: Request, project_id: str) -> tuple[Project, str | None]:
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project, user_id

    @router.get("/projects/{project_id}/shells")
    async def list_project_shells(request: Request, project_id: str) -> dict[str, Any]:
        """List the project's shells with their liveness, as the host reports them."""
        project, user_id = await owned_project(request, project_id)
        host_registry, conn, _ = await connected_project_host(request.app.state, project, user_id)
        require_project_shell_support(conn)
        result = await request_project_shell(host_registry, conn, op="list", project_id=project_id)
        _raise_if_failed(result.status, result.error)
        return {
            "object": "list",
            "data": [{**shell, "host_id": conn.host_id} for shell in result.shells],
        }

    @router.post(
        "/projects/{project_id}/shells",
        status_code=201,
        dependencies=[Depends(require_json_content_type)],
    )
    async def create_project_shell(request: Request, project_id: str) -> dict[str, Any]:
        """Start a shell on the project's default host in its default workspace."""
        project, user_id = await owned_project(request, project_id)
        host_registry, conn, workspace = await connected_project_host(
            request.app.state, project, user_id
        )
        require_project_shell_support(conn)
        result = await request_project_shell(
            host_registry, conn, op="create", project_id=project_id, workspace=workspace
        )
        _raise_if_failed(result.status, result.error)
        return {**result.shells[0], "host_id": conn.host_id}

    @router.delete("/projects/{project_id}/shells/{shell_id}")
    async def close_project_shell(
        request: Request, project_id: str, shell_id: str
    ) -> dict[str, Any]:
        """Kill one of the project's shells."""
        project, user_id = await owned_project(request, project_id)
        host_registry, conn, _ = await connected_project_host(request.app.state, project, user_id)
        require_project_shell_support(conn)
        result = await request_project_shell(
            host_registry, conn, op="close", project_id=project_id, shell_id=shell_id
        )
        _raise_if_failed(result.status, result.error)
        return {"id": shell_id, "object": "project.shell.deleted", "deleted": True}

    @router.websocket("/projects/{project_id}/shells/{shell_id}/attach")
    async def attach_project_shell(websocket: WebSocket, project_id: str, shell_id: str) -> None:
        """Attach the browser to a project shell through the host tunnel (owner only)."""
        user_id = auth_provider.get_user_id(websocket) if auth_provider is not None else None
        if auth_provider is not None and user_id is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION, reason="authentication required"
            )
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="not authorized")
        try:
            host_registry, conn, _ = await connected_project_host(
                websocket.app.state, project, user_id
            )
        except OmnigentError as exc:
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR, reason=str(exc)[:120]
            ) from exc
        set_current_user_id(user_id)
        await websocket.accept()
        await relay_terminal_ws(
            websocket,
            open_shell_attach(host_registry, conn, shell_id),
            span_attributes={
                "project.id": project_id,
                "terminal.id": shell_id,
                "terminal.mode": "project-shell",
            },
        )

    return router


def _raise_if_failed(result_status: str, error: str | None) -> None:
    if result_status != "ok":
        raise OmnigentError(
            error or "The host could not complete the shell request.", code=ErrorCode.INVALID_INPUT
        )

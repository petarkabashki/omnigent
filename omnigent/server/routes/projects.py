"""REST API routes for projects (``/v1/projects``).

Projects are first-class, owner-private containers that group sessions (see
``designs/PROJECTS_PRD.md``). These endpoints let the web UI create empty
projects, list them, rename them, and delete them. Session membership
(filing a session into a project) is managed on the sessions API via the
conversation store's ``project_id``.

Because projects are owner-private and carry no ACL of their own, every handler
scopes to the requesting user: a caller only ever sees and mutates their own
projects. In single-user mode (no auth provider) the owner is ``None`` and all
projects share that scope.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import _require_access_sync, require_user
from omnigent.server.routes._sessions.orchestration import _finish_archive
from omnigent.server.routes.project_shells import close_deleted_project_shells
from omnigent.server.routes.sessions import announce_projects_changed
from omnigent.server.schemas import (
    CreateProjectRequest,
    ProjectOrderRequest,
    ProjectOrderResponse,
    UpdateProjectRequest,
)
from omnigent.server.session_aliases import release_project_roles
from omnigent.stores.conversation_store import ConversationStore, pinned_label_key
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import (
    UNSET,
    ProjectOrderPreference,
    ProjectStore,
    apply_project_order,
    project_path,
)

logger = logging.getLogger(__name__)


def _to_response(project: Project, path: str | None = None) -> dict[str, Any]:
    """Convert a :class:`Project` entity to a ``ProjectObject`` response dict.

    :param project: The entity to convert.
    :param path: The computed namespace path, when known.
    :returns: Dict matching the :class:`ProjectObject` shape.
    """
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "config": project.config,
        "parent_id": project.parent_id,
        "path": path,
    }


def create_projects_router(
    project_store: ProjectStore,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
    runner_router: Any = None,
) -> APIRouter:
    """Build the projects router (``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :param conversation_store: Holds session membership; needed to delete a
        project together with its sessions.
    :param permission_store: Session permissions, or ``None`` when unchecked.
    :param runner_router: Stops the runners of archived sessions.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _require_modifiable(
        session_ids: Iterable[str],
        user_id: str | None,
        store: ConversationStore,
        approved: set[str],
    ) -> None:
        """Apply PATCH's archive/unfile check to each session not yet approved."""
        for session_id in session_ids:
            if session_id in approved:
                continue
            try:
                _require_access_sync(user_id, session_id, LEVEL_OWNER, permission_store, store)
            except OmnigentError as exc:
                if exc.code not in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
                    raise
                raise OmnigentError(
                    "This project holds a session you can't modify, so it wasn't deleted. "
                    "Nothing was changed.",
                    code=ErrorCode.FORBIDDEN,
                ) from exc
            approved.add(session_id)

    async def _delete_with_sessions(
        request: Request,
        store: ConversationStore,
        project_id: str,
        user_id: str | None,
    ) -> dict[str, Any]:
        """Delete a project and its sessions' membership, then run the side effects."""
        # Checked before the write lock so it isn't held for N permission reads;
        # the transaction re-checks only sessions filed since.
        approved: set[str] = set()
        role_path = await asyncio.to_thread(project_store.get_path, project_id, user_id=user_id)
        member_ids = await asyncio.to_thread(
            project_store.member_session_ids,
            project_id,
            user_id=user_id,
            conversation_store=store,
        )
        await asyncio.to_thread(_require_modifiable, member_ids, user_id, store, approved)
        sweep = await asyncio.to_thread(
            project_store.delete_with_sessions,
            project_id,
            user_id=user_id,
            conversation_store=store,
            authorize=lambda ids: _require_modifiable(ids, user_id, store, approved),
        )
        if sweep is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

        # Committed: a failure from here on must not fail the delete or skip the
        # remaining sessions. Already-archived members had these effects then.
        host_registry = getattr(request.app.state, "host_registry", None)
        for session_id in sweep.newly_archived_ids:
            try:
                _finish_archive(session_id, store, runner_router, host_registry)
            except Exception:
                logger.exception("Post-archive cleanup failed for session %s", session_id)

        def drop_pins() -> None:
            pin_key = pinned_label_key(user_id)
            for session_id in sweep.newly_archived_ids:
                try:
                    store.delete_label(session_id, pin_key)
                except Exception:
                    logger.exception("Dropping the pin failed for session %s", session_id)

        await asyncio.to_thread(drop_pins)
        await asyncio.to_thread(
            release_project_roles,
            store,
            project_store,
            project_id,
            session_ids=sweep.session_ids,
            user_id=user_id,
            role_path=role_path or "",
        )
        announce_projects_changed(user_id)
        return {
            "id": project_id,
            "object": "project.deleted",
            "deleted": True,
            "session_ids": list(sweep.session_ids),
            "updated_at": sweep.updated_at,
        }

    def _find(path: str, user_id: str | None) -> list[dict[str, Any]]:
        """Projects at ``path`` (tree walk, then legacy flat name) as response dicts.

        :raises OmnigentError: ``CONFLICT`` when sibling projects differing only by
            case match.
        """
        found = project_store.find_by_path(path.strip(), user_id=user_id)
        if len(found) > 1:
            ids = ", ".join(p.id for p in found)
            raise OmnigentError(
                f"Path {path!r} matches several projects: {ids}", code=ErrorCode.CONFLICT
            )
        return [_to_response(p, project_store.get_path(p.id, user_id=user_id)) for p in found]

    @router.post("/projects")
    async def create_project(
        request: Request,
        body: CreateProjectRequest,
    ) -> dict[str, Any]:
        """Create a new, empty project owned by the caller.

        :param request: The incoming request, used to identify the user.
        :param body: Project payload (name).
        :returns: The created project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409
            if the caller already has a project with this name.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.create,
            uuid.uuid4().hex,
            body.name,
            user_id,
            body.config,
            body.parent_id,
        )
        # Push the change to the owner's other connected clients so their
        # sidebars pick up the new folder without a reload.
        announce_projects_changed(user_id)
        path = await asyncio.to_thread(project_store.get_path, project.id, user_id=user_id)
        return _to_response(project, path)

    @router.get("/projects")
    async def list_projects(
        request: Request,
        path: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List the caller's projects, or the one at ``?path=a/b``.

        :param request: The incoming request, used to identify the user.
        :param path: When set, tree-walk this namespace path (case-insensitive
            segments) instead of listing everything.
        :returns: ``{"object": "list", "data": [...]}``; empty when ``path`` matches
            nothing.
        :raises OmnigentError: 401 if unauthenticated in multi-user mode, 409 if
            ``path`` matches several sibling projects that differ only by case, or both
            a tree project and a different legacy ``a/b``-named one.
        """
        user_id = require_user(request, auth_provider)

        def read_projects() -> list[dict[str, Any]]:
            if path is not None:
                return _find(path, user_id)
            projects = project_store.list(user_id=user_id)
            by_id = {p.id: p for p in projects}
            ordered = apply_project_order(
                projects,
                project_store.get_order(user_id=user_id),
                project_id=lambda p: p.id,
                project_name=lambda p: p.name,
            )
            return [_to_response(p, project_path(p, by_id)) for p in ordered]

        return {"object": "list", "data": await asyncio.to_thread(read_projects)}

    @router.get(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def get_project_order(request: Request) -> ProjectOrderPreference:
        """Read the caller's custom order."""
        user_id = require_user(request, auth_provider)
        return await asyncio.to_thread(project_store.get_order_preference, user_id=user_id)

    @router.put(
        "/projects/order", response_model=None, responses={200: {"model": ProjectOrderResponse}}
    )
    async def save_project_order(
        request: Request, body: ProjectOrderRequest
    ) -> ProjectOrderPreference:
        """Save the caller's order and notify their connected clients."""
        user_id = require_user(request, auth_provider)
        preference = await asyncio.to_thread(
            project_store.save_order, body.ordered_project_ids, user_id=user_id
        )
        announce_projects_changed(user_id)
        return preference

    @router.get("/projects/resolve")
    async def resolve_project(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Resolve a namespace path (``a/b``) to exactly one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param path: Namespace path; segments match case-insensitively.
        :returns: The project as a serialized dict (with its ``path``).
        :raises OmnigentError: 401 if unauthenticated, 404 if nothing matches, 409
            if the path is ambiguous.
        """
        user_id = require_user(request, auth_provider)
        found = await asyncio.to_thread(_find, path, user_id)
        if not found:
            raise OmnigentError(f"No project at path {path!r}", code=ErrorCode.NOT_FOUND)
        return found[0]

    @router.get("/projects/{project_id}")
    async def get_project(request: Request, project_id: str) -> dict[str, Any]:
        """Return one of the caller's projects.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to fetch.
        :returns: The project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        path = await asyncio.to_thread(project_store.get_path, project_id, user_id=user_id)
        return _to_response(project, path)

    @router.patch("/projects/{project_id}")
    async def update_project(
        request: Request,
        project_id: str,
        body: UpdateProjectRequest,
    ) -> dict[str, Any]:
        """Update one of the caller's projects (e.g. rename).

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: Fields to change; ``None`` fields are left unchanged.
        :returns: The updated project as a serialized dict.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found / not
            owned, 409 if the new name collides with another of the caller's
            projects.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.update,
            project_id,
            user_id=user_id,
            name=body.name,
            config=body.config,
            parent_id=body.parent_id if "parent_id" in body.model_fields_set else UNSET,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # A rename/config change is only ever seen by other connected clients
        # (another tab, the mobile app) if it is pushed; nothing else refreshes
        # their projects cache until a full reload.
        announce_projects_changed(user_id)
        path = await asyncio.to_thread(project_store.get_path, project_id, user_id=user_id)
        return _to_response(project, path)

    @router.delete("/projects/{project_id}")
    async def delete_project(
        request: Request,
        project_id: str,
        archive_sessions: bool = Query(
            default=False,
            description=(
                "Also archive and unfile every member session in one transaction; "
                "a refusal or failure changes nothing."
            ),
        ),
    ) -> dict[str, Any]:
        """Delete one of the caller's projects.

        Member sessions are not deleted. By default they are left for the caller
        to unfile (clearing their ``project_id``); ``archive_sessions`` archives
        and unfiles them atomically with the delete.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to delete.
        :param archive_sessions: Sweep the member sessions in the same transaction.
        :returns: ``{"id": ..., "object": "project.deleted", "deleted": True}``,
            plus ``session_ids`` and ``updated_at`` with ``archive_sessions``.
        :raises OmnigentError: 401 if unauthenticated, 403 if a member can't be
            modified by the caller, 404 if not found / not owned by the caller,
            409 if the project has children or its members are in a separate
            database.
        """
        user_id = require_user(request, auth_provider)
        doomed = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if archive_sessions:
            if conversation_store is None:
                raise OmnigentError(
                    "Deleting a project with its sessions is not supported by this server",
                    code=ErrorCode.INVALID_INPUT,
                )
            deleted_with_sessions = await _delete_with_sessions(
                request, conversation_store, project_id, user_id
            )
            await close_deleted_project_shells(request.app.state, doomed, user_id)
            return deleted_with_sessions
        role_path = await asyncio.to_thread(project_store.get_path, project_id, user_id=user_id)
        deleted = await asyncio.to_thread(project_store.delete, project_id, user_id=user_id)
        if not deleted:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        # The members stay filed for the caller to unfile, but their namespace is gone.
        if conversation_store is not None:
            await asyncio.to_thread(
                release_project_roles,
                conversation_store,
                project_store,
                project_id,
                session_ids=(),
                user_id=user_id,
                role_path=role_path or "",
            )
        # Drop the folder from the owner's other connected clients live.
        announce_projects_changed(user_id)
        await close_deleted_project_shells(request.app.state, doomed, user_id)
        return {"id": project_id, "object": "project.deleted", "deleted": True}

    return router

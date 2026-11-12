"""Saved views: owner-private named specs with params, resolved as the caller.

A saved view stores a query, never rows, and only its owner can list, read, change,
export or resolve it. Another user's view id answers 404, the same as a missing one.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from omnigent.entities.saved_view import SavedView
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes.views import _invalid, _json_object, resolve_source
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.saved_view_store import SqlAlchemySavedViewStore
from omnigent.views import saved
from omnigent.views.spec import MAX_SPEC_BYTES

_PARAM_QUERY_PREFIX = "p."
_JSON_BODY = [Depends(require_json_content_type)]


def _view_json(view: SavedView) -> dict[str, Any]:
    return {
        "id": view.id,
        "name": view.name,
        "description": view.description,
        "params": view.params,
        "spec": view.spec,
        "project_id": view.project_id,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


def _bad(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _not_found(view_id: str) -> OmnigentError:
    return OmnigentError(f"view {view_id} not found", code=ErrorCode.NOT_FOUND)


def _validated(
    name: Any, description: Any, params: Any, spec: Any
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Checked fields for storage: the spec must bind with every declared param."""
    if not isinstance(name, str) or not name.strip() or len(name) > saved.MAX_NAME_CHARS:
        raise _bad(f"name must be 1 to {saved.MAX_NAME_CHARS} characters")
    if not isinstance(description, str) or len(description) > saved.MAX_DESCRIPTION_CHARS:
        raise _bad(f"description must be at most {saved.MAX_DESCRIPTION_CHARS} characters")
    try:
        declared = saved.parse_params(params)
        checked = saved.check(spec, declared)
    except ValueError as error:
        raise _invalid(error) from error
    if len(json.dumps(checked).encode("utf-8")) > MAX_SPEC_BYTES:
        raise _bad(f"spec is larger than {MAX_SPEC_BYTES} bytes")
    stored_params = {
        param: decl.model_dump(mode="json", exclude_none=True) for param, decl in declared.items()
    }
    return name, description, stored_params, checked


def _query_values(request: Request, params: dict[str, Any]) -> dict[str, Any]:
    """``?p.<name>=<value>`` query arguments, typed by the view's declared params."""
    declared = saved.parse_params(params)
    values: dict[str, Any] = {}
    for key, raw in request.query_params.multi_items():
        if not key.startswith(_PARAM_QUERY_PREFIX):
            continue
        name = key[len(_PARAM_QUERY_PREFIX) :]
        if name not in declared:
            raise _bad(f"the view has no param {name!r}")
        if name in values:
            raise _bad(f"param {name!r} is given more than once")
        try:
            values[name] = saved.coerce(declared[name], raw, name)
        except ValueError as error:
            raise _bad(str(error)) from error
    return values


def create_saved_views_router(
    conversation_store: ConversationStore,
    project_store: ProjectStore | None = None,
    host_store: HostStore | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """CRUD, YAML export/import and resolve for the caller's own saved views."""
    router = APIRouter()

    def _store() -> SqlAlchemySavedViewStore:
        store = project_store.saved_views if project_store is not None else None
        if store is None:
            raise OmnigentError(
                "saved views are not available on this server", code=ErrorCode.NOT_FOUND
            )
        return store

    def _check_project(project_id: Any, user_id: str | None) -> str | None:
        if project_id is None:
            return None
        if not isinstance(project_id, str) or project_store is None:
            raise _bad("project_id must be a project id")
        if project_store.get(project_id, user_id=user_id) is None:
            raise OmnigentError(f"project {project_id} not found", code=ErrorCode.NOT_FOUND)
        return project_id

    def _owned(view_id: str, user_id: str | None) -> SavedView:
        view = _store().get(view_id, user_id=user_id)
        if view is None:
            raise _not_found(view_id)
        return view

    @router.get("/views")
    async def list_views(request: Request, project_id: str | None = None) -> dict[str, Any]:
        """The caller's saved views by name, optionally only those filed under a project."""
        user_id = require_user(request, auth_provider)
        views = _store().list_owned(user_id=user_id, project_id=project_id)
        return {"views": [_view_json(view) for view in views]}

    @router.post("/views", dependencies=_JSON_BODY)
    async def create_view(request: Request) -> dict[str, Any]:
        """Save a view; 409 when the caller already has a view of that name."""
        user_id = require_user(request, auth_provider)
        body = await _json_object(request)
        name, description, params, spec = _validated(
            body.get("name"), body.get("description", ""), body.get("params", {}), body.get("spec")
        )
        view = _store().create(
            name=name,
            description=description,
            params=params,
            spec=spec,
            project_id=_check_project(body.get("project_id"), user_id),
            user_id=user_id,
        )
        return _view_json(view)

    @router.post("/views/import", dependencies=_JSON_BODY)
    async def import_view(request: Request) -> dict[str, Any]:
        """Save a view from the YAML exchange format in ``{"yaml": "..."}``."""
        user_id = require_user(request, auth_provider)
        body = await _json_object(request)
        text = body.get("yaml")
        if not isinstance(text, str):
            raise _bad("body needs a 'yaml' string")
        try:
            document = saved.from_yaml(text)
        except ValueError as error:
            raise _invalid(error) from error
        name, description, params, spec = _validated(
            body.get("name", document.name), document.description, document.params, document.spec
        )
        view = _store().create(
            name=name,
            description=description,
            params=params,
            spec=spec,
            project_id=_check_project(body.get("project_id"), user_id),
            user_id=user_id,
        )
        return _view_json(view)

    @router.get("/views/{view_id}")
    async def get_view(request: Request, view_id: str) -> dict[str, Any]:
        """One of the caller's saved views."""
        user_id = require_user(request, auth_provider)
        return _view_json(_owned(view_id, user_id))

    @router.put("/views/{view_id}", dependencies=_JSON_BODY)
    async def update_view(request: Request, view_id: str) -> dict[str, Any]:
        """Change the given fields; the result must still validate as a whole."""
        user_id = require_user(request, auth_provider)
        body = await _json_object(request)
        unknown = set(body) - {"name", "description", "params", "spec", "project_id"}
        if unknown:
            raise _bad(f"unknown fields: {sorted(unknown)}")
        current = _owned(view_id, user_id)
        name, description, params, spec = _validated(
            body.get("name", current.name),
            body.get("description", current.description),
            body.get("params", current.params),
            body.get("spec", current.spec),
        )
        changes: dict[str, Any] = {
            "name": name,
            "description": description,
            "params": params,
            "spec": spec,
        }
        if "project_id" in body:
            changes["project_id"] = _check_project(body["project_id"], user_id)
        view = _store().update(view_id, user_id=user_id, **changes)
        if view is None:
            raise _not_found(view_id)
        return _view_json(view)

    @router.delete("/views/{view_id}")
    async def delete_view(request: Request, view_id: str) -> dict[str, Any]:
        """Delete one of the caller's saved views."""
        user_id = require_user(request, auth_provider)
        if not _store().delete(view_id, user_id=user_id):
            raise _not_found(view_id)
        return {"id": view_id, "deleted": True}

    @router.get("/views/{view_id}/export")
    async def export_view(request: Request, view_id: str) -> Response:
        """The view in the YAML exchange format; it holds the query, never rows."""
        user_id = require_user(request, auth_provider)
        view = _owned(view_id, user_id)
        text = saved.to_yaml(view.name, view.description, view.params, view.spec)
        return Response(content=text, media_type="application/yaml")

    @router.get("/views/{view_id}/resolve")
    async def resolve_saved_view(request: Request, view_id: str) -> dict[str, Any]:
        """Bind ``?p.<name>=`` values, then resolve the view's source as the caller."""
        user_id = require_user(request, auth_provider)
        view = _owned(view_id, user_id)
        values = _query_values(request, view.params)
        try:
            bound = saved.bind(view.spec, saved.parse_params(view.params), values)
        except ValueError as error:
            raise _invalid(error) from error
        result = await resolve_source(
            bound.view.source,
            user_id,
            conversation_store,
            project_store,
            host_store,
            auth_provider,
            permission_store,
        )
        return {
            "id": view.id,
            "spec": bound.model_dump(mode="json", exclude_none=True),
            **result,
        }

    return router

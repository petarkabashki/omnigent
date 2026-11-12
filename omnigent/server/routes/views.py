"""Agent-built views: validate a spec and resolve a named source for the caller."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store import ProjectStore
from omnigent.views.sources import HubSources, SourceError
from omnigent.views.spec import Source, parse_spec

_SOURCE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Source)


def _invalid(error: ValueError) -> OmnigentError:
    if isinstance(error, ValidationError):
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:5]
        )
        return OmnigentError(f"invalid view spec: {reasons}", code=ErrorCode.INVALID_INPUT)
    return OmnigentError(f"invalid view spec: {error}", code=ErrorCode.INVALID_INPUT)


def create_views_router(
    conversation_store: ConversationStore,
    project_store: ProjectStore | None = None,
    host_store: HostStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """``POST /views/validate`` and ``POST /views/resolve``, scoped to the calling user."""
    router = APIRouter()

    @router.post("/views/validate")
    async def validate_view(request: Request) -> dict[str, Any]:
        """Validate a spec (JSON object or string); returns the normalized spec."""
        require_user(request, auth_provider)
        body = await request.json()
        raw = body.get("spec") if isinstance(body, dict) else None
        if raw is None:
            raise OmnigentError("body needs a 'spec'", code=ErrorCode.INVALID_INPUT)
        try:
            spec = parse_spec(raw)
        except ValueError as error:
            raise _invalid(error) from error
        return {"spec": spec.model_dump(mode="json", exclude_none=True)}

    @router.post("/views/resolve")
    async def resolve_view(request: Request) -> dict[str, Any]:
        """Rows of one named source, with the row fields of ``agentic_base.query``."""
        user_id = require_user(request, auth_provider)
        body = await request.json()
        try:
            source = _SOURCE_ADAPTER.validate_python(
                body.get("source") if isinstance(body, dict) else None
            )
        except ValueError as error:
            raise _invalid(error) from error

        def _host_name(host_id: str) -> str | None:
            # Only the caller's own hosts are named; a shared session's host stays null.
            host = host_store.get_host(host_id) if host_store is not None else None
            if host is None or (user_id is not None and host.user_id != user_id):
                return None
            return host.name

        sources = HubSources(conversation_store, project_store, user_id, host_name=_host_name)
        try:
            resolved = await asyncio.to_thread(sources.resolve, source)
        except SourceError as error:
            raise OmnigentError(str(error), code=ErrorCode.NOT_FOUND) from error
        return {
            "origin": sources.origin,
            "source": source.name,
            "rows": resolved.rows,
            "truncated": resolved.truncated,
        }

    return router

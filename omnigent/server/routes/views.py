"""Agent-built views: validate a spec and resolve a named source for the caller."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.views.sources import HubSources, SourceError
from omnigent.views.spec import MAX_SPEC_BYTES, Source, parse_spec

_SOURCE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Source)
# A body holds one spec or source plus its JSON envelope (a string spec is escaped once).
_MAX_BODY_BYTES = 2 * MAX_SPEC_BYTES + 1024


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


async def _json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object; malformed, deep or oversized bodies are a 400."""
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise OmnigentError("request body is too large", code=ErrorCode.INVALID_INPUT)
    try:
        body = json.loads(raw, parse_constant=_refuse_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise OmnigentError(
            "request body is not valid JSON", code=ErrorCode.INVALID_INPUT
        ) from error
    if not isinstance(body, dict):
        raise OmnigentError("request body must be a JSON object", code=ErrorCode.INVALID_INPUT)
    return body


def _invalid(error: ValueError) -> OmnigentError:
    if isinstance(error, ValidationError):
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:5]
        )
        return OmnigentError(f"invalid view spec: {reasons}", code=ErrorCode.INVALID_INPUT)
    return OmnigentError(f"invalid view spec: {error}", code=ErrorCode.INVALID_INPUT)


async def resolve_source(
    source: Source,
    user_id: str | None,
    conversation_store: ConversationStore,
    project_store: ProjectStore | None,
    host_store: HostStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> dict[str, Any]:
    """Rows of ``source`` as ``user_id`` may read them, as ``POST /views/resolve`` returns."""

    def _host_name(host_id: str) -> str | None:
        # Only the caller's own hosts are named; a shared session's host stays null.
        host = host_store.get_host(host_id) if host_store is not None else None
        if host is None or (user_id is not None and host.user_id != user_id):
            return None
        return host.name

    def _can_read(conversation: Conversation) -> bool:
        # With auth or permissions off, every caller owns every session.
        if auth_provider is None or permission_store is None:
            return True
        return check_session_access(
            user_id,
            conversation.id,
            LEVEL_READ,
            permission_store,
            conversation_store,
            conversation=conversation,
        )

    sources = HubSources(
        conversation_store, project_store, user_id, _can_read, host_name=_host_name
    )
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


def create_views_router(
    conversation_store: ConversationStore,
    project_store: ProjectStore | None = None,
    host_store: HostStore | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """``POST /views/validate`` and ``POST /views/resolve``, scoped to the calling user."""
    router = APIRouter(dependencies=[Depends(require_json_content_type)])

    @router.post("/views/validate")
    async def validate_view(request: Request) -> dict[str, Any]:
        """Validate a spec (JSON object or string); returns the normalized spec."""
        require_user(request, auth_provider)
        raw = (await _json_object(request)).get("spec")
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
        body = await _json_object(request)
        try:
            source = _SOURCE_ADAPTER.validate_python(body.get("source"))
        except ValueError as error:
            raise _invalid(error) from error

        return await resolve_source(
            source,
            user_id,
            conversation_store,
            project_store,
            host_store,
            auth_provider,
            permission_store,
        )

    return router

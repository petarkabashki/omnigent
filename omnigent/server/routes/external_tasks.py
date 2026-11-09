"""Read-only session rows for work that runs outside Omnigent (e.g. systemd units)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator

from omnigent.db.utils import builtin_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import resolve_project_session_create
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.external_task import (
    EXTERNAL_DETAIL_LABEL_KEY,
    EXTERNAL_EXIT_CODE_LABEL_KEY,
    EXTERNAL_HOST_LABEL_KEY,
    EXTERNAL_KIND_LABEL_KEY,
    EXTERNAL_KIND_SYSTEMD_UNIT,
    EXTERNAL_STATE_LABEL_KEY,
    EXTERNAL_STATE_TO_LIVE_STATUS,
    EXTERNAL_UNIT_LABEL_KEY,
    is_external_task,
)

# Rows need some agent to satisfy the session listing; reuse the imports' built-in.
_PLACEHOLDER_AGENT_NAME = "claude-native-ui"
_MAX_DETAIL_CHARS = 2000


class ExternalTaskCreateRequest(BaseModel):
    """Body of ``POST /v1/external-tasks``."""

    title: str = Field(min_length=1, max_length=500)
    unit: str = Field(min_length=1, max_length=255)
    host_id: str | None = None
    project_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    status: str | None = None

    @field_validator("title", "unit")
    @classmethod
    def strip_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ExternalTaskUpdateRequest(BaseModel):
    """Body of ``PATCH /v1/external-tasks/{session_id}``."""

    status: str
    exit_code: int | None = None
    detail: str | None = Field(default=None, max_length=_MAX_DETAIL_CHARS)
    host_id: str | None = None


class ExternalTaskResponse(BaseModel):
    """The external task row as returned by create and update."""

    id: str
    session_id: str
    title: str | None
    status: str
    state: str | None
    exit_code: int | None
    detail: str | None
    unit: str | None
    host_id: str | None
    project_id: str | None
    labels: dict[str, str]
    created: bool = False


def external_task_id(owner: str | None, host_id: str | None, unit: str) -> str:
    """Derive a stable session id so a repeated create finds the same row."""
    return hashlib.sha256(
        f"external-task:{owner or ''}:{host_id or ''}:{unit}".encode()
    ).hexdigest()[:32]


def _live_status_for(state: str) -> str:
    try:
        return EXTERNAL_STATE_TO_LIVE_STATUS[state]
    except KeyError:
        raise OmnigentError(
            f"Unknown status {state!r}; expected one of {sorted(EXTERNAL_STATE_TO_LIVE_STATUS)}",
            code=ErrorCode.INVALID_INPUT,
        ) from None


def _response(conversation: Any, *, created: bool = False) -> ExternalTaskResponse:
    labels = dict(conversation.labels or {})
    exit_code = labels.get(EXTERNAL_EXIT_CODE_LABEL_KEY)
    return ExternalTaskResponse(
        id=conversation.id,
        session_id=conversation.id,
        title=conversation.title,
        status=conversation.live_status or "idle",
        state=labels.get(EXTERNAL_STATE_LABEL_KEY),
        exit_code=int(exit_code) if exit_code not in (None, "") else None,
        detail=labels.get(EXTERNAL_DETAIL_LABEL_KEY),
        unit=labels.get(EXTERNAL_UNIT_LABEL_KEY),
        host_id=labels.get(EXTERNAL_HOST_LABEL_KEY),
        project_id=conversation.project_id,
        labels=labels,
        created=created,
    )


def create_external_tasks_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    project_store: ProjectStore | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Create the external task row router."""
    router = APIRouter()

    async def _load(request: Request, session_id: str) -> Any:
        user_id = require_user(request, auth_provider)
        await require_access(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None or not is_external_task(conversation.labels):
            raise OmnigentError("External task not found", code=ErrorCode.NOT_FOUND)
        return conversation

    async def _write_state(
        session_id: str,
        state: str,
        exit_code: int | None,
        detail: str | None,
        host_id: str | None = None,
    ) -> None:
        live_status = _live_status_for(state)
        labels = {
            EXTERNAL_STATE_LABEL_KEY: state,
            EXTERNAL_EXIT_CODE_LABEL_KEY: "" if exit_code is None else str(exit_code),
            EXTERNAL_DETAIL_LABEL_KEY: detail or "",
        }
        if host_id is not None:
            labels[EXTERNAL_HOST_LABEL_KEY] = host_id
        await asyncio.to_thread(conversation_store.set_labels, session_id, labels)
        await asyncio.to_thread(
            conversation_store.set_session_live_status, session_id, live_status
        )

    @router.post(
        "/external-tasks",
        response_model=ExternalTaskResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def create_external_task(
        body: ExternalTaskCreateRequest, request: Request, response: Response
    ) -> ExternalTaskResponse:
        """Create (or return the existing) read-only row for an external unit."""
        user_id = require_user(request, auth_provider)
        state = body.status or "queued"
        _live_status_for(state)
        if body.host_id is not None and host_store is not None:
            await asyncio.to_thread(
                resolve_host_owner, user_id=user_id, host_id=body.host_id, host_store=host_store
            )
        session_id = external_task_id(user_id, body.host_id, body.unit)
        existing = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if existing is not None:
            await require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            return _response(existing)

        agent_id = builtin_agent_id(_PLACEHOLDER_AGENT_NAME)
        if await asyncio.to_thread(agent_store.get, agent_id) is None:
            raise OmnigentError(
                "The built-in agent backing external task rows is unavailable",
                code=ErrorCode.INTERNAL_ERROR,
            )
        create_kwargs: dict[str, Any] = {"agent_id": agent_id}
        if body.project_id is not None:
            create_kwargs["project_id"] = body.project_id
        resolved = await resolve_project_session_create(
            body=SessionCreateRequest(**create_kwargs),
            user_id=user_id,
            project_store=project_store,
        )
        try:
            conversation = await asyncio.to_thread(
                conversation_store.create_conversation,
                title=body.title,
                agent_id=agent_id,
                conversation_id=session_id,
                project_id=resolved.project_id,
            )
        except ConversationAlreadyExistsError:
            raced = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if raced is None:
                raise
            return _response(raced)
        try:
            labels = {
                **body.labels,
                EXTERNAL_KIND_LABEL_KEY: EXTERNAL_KIND_SYSTEMD_UNIT,
                EXTERNAL_UNIT_LABEL_KEY: body.unit,
            }
            if body.host_id is not None:
                labels[EXTERNAL_HOST_LABEL_KEY] = body.host_id
            await asyncio.to_thread(conversation_store.set_labels, session_id, labels)
            await _write_state(session_id, state, None, None)
            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(permission_store.grant, user_id, session_id, LEVEL_OWNER)
        except Exception:
            await conversation_store.delete_conversation(session_id)
            raise
        created = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        response.status_code = 201
        return _response(created or conversation, created=True)

    @router.patch(
        "/external-tasks/{session_id}",
        response_model=ExternalTaskResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def update_external_task(
        session_id: str, body: ExternalTaskUpdateRequest, request: Request
    ) -> ExternalTaskResponse:
        """Update an external task row's status, exit code and detail."""
        await _load(request, session_id)
        _live_status_for(body.status)
        if body.host_id is not None and host_store is not None:
            await asyncio.to_thread(
                resolve_host_owner,
                user_id=require_user(request, auth_provider),
                host_id=body.host_id,
                host_store=host_store,
            )
        await _write_state(session_id, body.status, body.exit_code, body.detail, body.host_id)
        updated = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if updated is None:
            raise OmnigentError("External task not found", code=ErrorCode.NOT_FOUND)
        return _response(updated)

    return router

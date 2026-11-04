"""Owner gestures that label a routing decision's outcome."""

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.routing_outcomes import record_accept
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class AcceptSessionResponse(BaseModel):
    """Whether the accept reached the outcome stream."""

    recorded: bool


def register_outcomes_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the accept gesture."""

    @router.post(
        "/sessions/{session_id}/accept",
        response_model=AcceptSessionResponse,
    )
    async def accept_session(request: Request, session_id: str) -> AcceptSessionResponse:
        """Record that the owner accepts this session's result.

        Requires owner access. ``recorded`` is false when the session has no
        logged routing decision (shadow routing off), which is not an error.
        """
        await require_access_and_level(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        if await asyncio.to_thread(conversation_store.get_conversation, session_id) is None:
            raise session_not_found()
        recorded = await asyncio.to_thread(record_accept, session_id, "owner")
        return AcceptSessionResponse(recorded=recorded)

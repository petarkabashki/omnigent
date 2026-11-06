"""Gates for an in-place engine switch: ``reset`` with ``engine``.

Everything here runs ahead of the reset's own gates, so a refusal stops no turn
and reaches no runner.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from sqlalchemy.exc import StatementError

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_availability import harness_launch_availability
from omnigent.models.model_override import model_family_mismatch, validate_model_override
from omnigent.server.bundles import agent_needs_own_copy
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.routes._sessions.helpers import (
    _presentation_labels_for_agent,
    _resolve_harness,
    _resolve_llm_model,
    _same_provider_family,
)
from omnigent.server.routes._sessions.reset import EngineTarget
from omnigent.server.routes.sandbox_inference import CONFIGURED_SESSION_HARNESS_LOCKED
from omnigent.server.schemas import ResetEngine
from omnigent.util.reasoning_effort import EFFORT_CLEAR_VALUES, EFFORT_VALUES, validate_effort
from omnigent.util.uuid import InvalidUuidError


def _refused(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _explicit_model(raw_model: str, harness: str) -> str | None:
    """Validate a requested model for ``harness``; a clear word means the target's default."""
    if raw_model.strip().lower() in EFFORT_CLEAR_VALUES:
        return None
    try:
        model = validate_model_override(raw_model)
    except ValueError as exc:
        raise _refused(f"invalid model: {exc}") from exc
    mismatch = model_family_mismatch(harness, model)
    if mismatch is not None:
        raise _refused(mismatch)
    return model


def _explicit_effort(raw_effort: str) -> str | None:
    """Validate a requested effort; a clear word means the target's default."""
    if raw_effort in EFFORT_CLEAR_VALUES:
        return None
    try:
        return validate_effort(raw_effort, "engine", EFFORT_VALUES)
    except ValueError as exc:
        raise _refused(f"invalid reasoning_effort: {exc}") from exc


def _require_installed(
    harness: str, conv: Conversation, user_id: str | None, host_store: Any
) -> None:
    """Refuse a harness the session's own bound host reports as unavailable."""
    if conv.host_id is None or host_store is None:
        return
    host = host_store.get_host(conv.host_id)
    if host is None or host.user_id != user_id:
        return
    available, reason = harness_launch_availability(harness, host.configured_harnesses)
    if available is False:
        raise _refused(f"{harness} is not available on the session's host: {reason}")


async def resolve_engine_target(
    *,
    conv: Conversation,
    engine: ResetEngine,
    user_id: str | None,
    agent_store: Any,
    agent_cache: Any,
    permission_store: Any,
    conversation_store: Any,
    host_store: Any,
) -> EngineTarget:
    """
    Validate a switch and work out the binding it will write.

    :param conv: The session, as read for this request.
    :param engine: The request's target.
    :param user_id: The caller, already checked as the session's owner.
    :returns: The target and the source it replaces.
    :raises OmnigentError: ``INVALID_INPUT`` for every refusal.
    """
    if conv.parent_conversation_id is not None:
        raise _refused("A spawned session can't switch engine: its parent owns its spec.")
    if conv.inference_snapshot is not None:
        raise _refused(CONFIGURED_SESSION_HARNESS_LOCKED)
    try:
        target_agent = await validate_session_agent(
            user_id=user_id,
            agent_id=engine.agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
    except OmnigentError as exc:
        if exc.code not in (ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN):
            raise
        raise _refused(f"Unknown agent: {engine.agent_id}") from exc
    except StatementError as exc:
        # A malformed id fails at the bind; the server would otherwise answer 404.
        if not isinstance(exc.orig, InvalidUuidError):
            raise
        raise _refused(f"Unknown agent: {engine.agent_id}") from exc
    if agent_needs_own_copy(target_agent, user_id):
        raise _refused("Fork the session to use another user's agent.")

    def resolve() -> EngineTarget:
        return _resolve_target(
            conv, engine, target_agent, user_id, agent_store, agent_cache, host_store
        )

    return await asyncio.to_thread(resolve)


def _resolve_target(
    conv: Conversation,
    engine: ResetEngine,
    target_agent: Any,
    user_id: str | None,
    agent_store: Any,
    agent_cache: Any,
    host_store: Any,
) -> EngineTarget:
    source_harness = _resolve_harness(conv, agent_store=agent_store, agent_cache=agent_cache)
    source_model = _resolve_llm_model(conv, agent_store=agent_store, agent_cache=agent_cache)
    rebound = dataclasses.replace(
        conv,
        agent_id=target_agent.id,
        harness_override=None,
        model_override=None,
        reasoning_effort=None,
    )
    harness = _resolve_harness(rebound, agent_store=agent_store, agent_cache=agent_cache)
    if harness is None:
        raise _refused(f"Could not resolve a harness for agent {target_agent.id}.")
    if harness == source_harness:
        raise _refused(f"The session already runs {harness}; use the model selector.")
    _require_installed(harness, conv, user_id, host_store)
    source_agent = agent_store.get(conv.agent_id) if conv.agent_id else None
    keeps_settings = source_agent is not None and _same_provider_family(source_agent, target_agent)
    model_override = (
        _explicit_model(engine.model, harness)
        if engine.model is not None
        else (conv.model_override if keeps_settings else None)
    )
    reasoning_effort = (
        _explicit_effort(engine.reasoning_effort)
        if engine.reasoning_effort is not None
        else (conv.reasoning_effort if keeps_settings else None)
    )
    default_model = _resolve_llm_model(rebound, agent_store=agent_store, agent_cache=agent_cache)
    return EngineTarget(
        agent_id=target_agent.id,
        harness=harness,
        model=model_override or default_model,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
        presentation_labels=_presentation_labels_for_agent(target_agent),
        source_agent_id=conv.agent_id,
        source_harness=source_harness,
        source_model=conv.model_override or source_model,
    )

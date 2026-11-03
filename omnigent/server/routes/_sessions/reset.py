"""In-place context reset for a session (``POST /v1/sessions/{id}/reset``).

The session id, items and children stay; a ``context_reset`` boundary item
starts a new context epoch. The runner checks and drops its caches
(``prepare``), the server persists the boundary, then the runner drops any
history cached meanwhile and releases the harness (``commit``), and the server
publishes ``session.reset``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs
from omnigent.server.schemas import (
    ResetSessionRequest,
    ResetSessionResponse,
    ResetSummaryText,
    SessionResetEvent,
)
from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)


@dataclass
class _InFlightReset:
    """A running reset, shared with a concurrent retry of the same key."""

    stable_id: str | None
    result: asyncio.Future[ResetSessionResponse] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


#: Resets in progress on this replica, by session. New input is refused
#: while a session is listed here.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by globally unique session id
_reset_in_flight: dict[str, _InFlightReset] = {}

_BUSY_STATUSES = ("running", "waiting")
_STOP_WAIT_S = 10.0
_STOP_POLL_S = 0.2
#: Forward budget for reset controls; ``commit`` releases the harness process.
RESET_FORWARD_TIMEOUT_S = 30.0

RunnerForward = Callable[[dict[str, Any]], Awaitable[Any]]


def is_reset_in_flight(session_id: str) -> bool:
    """Return whether a context reset is in progress for ``session_id`` here."""
    return session_id in _reset_in_flight


def reset_stable_id(session_id: str, idempotency_key: str) -> str:
    """Return the boundary item's stable id for an idempotency key."""
    digest = hashlib.sha256(f"{session_id}:{idempotency_key}".encode()).hexdigest()
    return digest[:32]


def _unsupported(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _runner_error_code(body: str) -> str:
    """Map a runner 409 body (``{"error": "<code>"}``) to an error code."""
    try:
        code = json.loads(body).get("error")
    except (ValueError, AttributeError):
        code = None
    known = (
        ErrorCode.PENDING_INPUTS,
        ErrorCode.RESET_IN_PROGRESS,
        ErrorCode.RESET_UNSUPPORTED,
        ErrorCode.SESSION_BUSY,
    )
    return code if code in known else ErrorCode.RESET_IN_PROGRESS


async def run_context_reset(
    *,
    session_id: str,
    conv: Conversation,
    reset: ResetSessionRequest,
    conversation_store: ConversationStore,
    status_of: Callable[[], str | None],
    has_server_pending: Callable[[], Awaitable[bool]],
    drop_server_pending: Callable[[], Awaitable[None]],
    interrupt: Callable[[], Awaitable[None]],
    forward: RunnerForward,
    wake: Callable[[], Awaitable[bool]],
    native: bool,
    publish: Callable[[dict[str, Any]], object],
    dispatching: Callable[[], bool] = lambda: False,
    created_by: str | None = None,
) -> ResetSessionResponse:
    """
    Reset a session's model context in place.

    :param status_of: Returns the live status, e.g. ``"running"``.
    :param has_server_pending: Whether server-queued input exists.
    :param drop_server_pending: Discards server-queued input.
    :param interrupt: Stops the running turn.
    :param forward: POSTs a control event to the runner; ``None`` when unbound.
    :param wake: Relaunches a sleeping runner; ``True`` when one is bound after.
    :param native: Whether a native terminal harness drives the session.
    :param publish: Publishes an SSE payload on the session stream.
    :param dispatching: Whether a message dispatch is still in flight here.
    :returns: The new epoch and boundary item id.
    :raises OmnigentError: 409 for busy, pending input, an unsupported
        harness or a concurrent reset; 503 when no runner is reachable.
    """
    if reset.summary == "auto":
        raise _unsupported("summary 'auto' is not supported yet; use 'none' or {'text': ...}")
    if reset.pending == "carry":
        raise _unsupported("pending 'carry' is not supported yet; use 'reject' or 'drop'")
    stable_id = (
        reset_stable_id(session_id, reset.idempotency_key) if reset.idempotency_key else None
    )
    if stable_id is not None:
        existing = await asyncio.to_thread(conversation_store.get_item, session_id, stable_id)
        if existing is not None and existing.type == "context_reset":
            return ResetSessionResponse(
                session_id=session_id,
                epoch=existing.data.epoch,  # type: ignore[union-attr]
                boundary_item_id=existing.id,
            )
    if native:
        raise OmnigentError(
            "Reset in place is SDK-only for now; native support is planned.",
            code=ErrorCode.RESET_UNSUPPORTED,
        )
    running = _reset_in_flight.get(session_id)
    if running is not None:
        if stable_id is not None and running.stable_id == stable_id:
            return await asyncio.shield(running.result)
        raise OmnigentError(
            "A reset is already in progress for this session", code=ErrorCode.RESET_IN_PROGRESS
        )
    in_flight = _InFlightReset(stable_id)
    _reset_in_flight[session_id] = in_flight
    try:
        response = await _reset_in_place(
            session_id=session_id,
            conv=conv,
            reset=reset,
            stable_id=stable_id,
            conversation_store=conversation_store,
            status_of=status_of,
            has_server_pending=has_server_pending,
            drop_server_pending=drop_server_pending,
            interrupt=interrupt,
            forward=forward,
            wake=wake,
            publish=publish,
            dispatching=dispatching,
            created_by=created_by,
        )
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            in_flight.result.cancel()
        else:
            in_flight.result.set_exception(exc)
            in_flight.result.exception()  # A retry may never await it.
        raise
    else:
        in_flight.result.set_result(response)
        return response
    finally:
        _reset_in_flight.pop(session_id, None)


async def _reset_in_place(
    *,
    session_id: str,
    conv: Conversation,
    reset: ResetSessionRequest,
    stable_id: str | None,
    conversation_store: ConversationStore,
    status_of: Callable[[], str | None],
    has_server_pending: Callable[[], Awaitable[bool]],
    drop_server_pending: Callable[[], Awaitable[None]],
    interrupt: Callable[[], Awaitable[None]],
    forward: RunnerForward,
    wake: Callable[[], Awaitable[bool]],
    publish: Callable[[dict[str, Any]], object],
    dispatching: Callable[[], bool],
    created_by: str | None,
) -> ResetSessionResponse:
    # A message that passed the input gate before the reset registered may
    # still be on its way to the runner; let it land so the busy check sees it.
    await _wait_until(lambda: not dispatching(), "A message is still being delivered")
    await _ensure_idle(reset, status_of, interrupt)
    if await has_server_pending() or pending_inputs.has_pending(session_id):
        if reset.pending == "reject":
            raise OmnigentError(
                "The session has queued input; retry with pending='drop'",
                code=ErrorCode.PENDING_INPUTS,
            )
        await drop_server_pending()
        while pending_inputs.resolve_oldest(session_id) is not None:
            pass
    runner_reached = await _forward_reset(reset, forward, wake, conv)
    summary = reset.summary.text if isinstance(reset.summary, ResetSummaryText) else None
    item = await asyncio.to_thread(
        conversation_store.append_context_reset,
        session_id,
        response_id=f"reset_{int(time.time() * 1000)}",
        summary=summary,
        reason="api",
        stable_id=stable_id,
        created_by=created_by,
    )
    if runner_reached:
        await _commit_reset(session_id, forward)
    epoch = item.data.epoch  # type: ignore[union-attr]
    publish(
        SessionResetEvent(
            type="session.reset",
            conversation_id=session_id,
            epoch=epoch,
            boundary_item_id=item.id,
            summary_present=summary is not None,
        ).model_dump()
    )
    return ResetSessionResponse(session_id=session_id, epoch=epoch, boundary_item_id=item.id)


async def _wait_until(done: Callable[[], bool], busy_message: str) -> None:
    deadline = time.monotonic() + _STOP_WAIT_S
    while not done():
        if time.monotonic() >= deadline:
            raise OmnigentError(busy_message, code=ErrorCode.SESSION_BUSY)
        await asyncio.sleep(_STOP_POLL_S)


async def _ensure_idle(
    reset: ResetSessionRequest,
    status_of: Callable[[], str | None],
    interrupt: Callable[[], Awaitable[None]],
) -> None:
    if status_of() not in _BUSY_STATUSES:
        return
    if not reset.stop_running:
        raise OmnigentError(
            "The session is running; retry with stop_running=true", code=ErrorCode.SESSION_BUSY
        )
    await interrupt()
    await _wait_until(
        lambda: status_of() not in _BUSY_STATUSES, "The running turn did not stop in time"
    )


async def _forward_reset(
    reset: ResetSessionRequest,
    forward: RunnerForward,
    wake: Callable[[], Awaitable[bool]],
    conv: Conversation,
) -> bool:
    """Ask the runner to check and drop its caches; ``False`` when none is bound."""
    event = {"type": "reset", "phase": "prepare", "pending": reset.pending}
    result = await forward(event)
    if result is None and conv.runner_id is None:
        # No runner bound: nothing caches history, the next launch reads the boundary.
        return False
    if result is None and conv.host_id is not None and await wake():
        result = await forward(event)
    if result is None:
        raise OmnigentError(
            "Can't reset this session while its runner is unreachable.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if result.status_code == 200:
        return True
    if result.status_code == 204:
        raise OmnigentError(
            "reset not supported by this runner version", code=ErrorCode.RESET_UNSUPPORTED
        )
    if result.status_code == 409:
        raise OmnigentError(
            f"Runner refused the reset: {result.body[:200]}",
            code=_runner_error_code(result.body),
        )
    raise OmnigentError(
        f"Reset failed: runner returned {result.status_code}", code=ErrorCode.INTERNAL_ERROR
    )


async def _commit_reset(session_id: str, forward: RunnerForward) -> None:
    """Drop history a turn cached before the boundary landed, and release the harness.

    The boundary is already persisted, so a failure here is logged, not raised:
    the ``prepare`` step dropped the caches and new input stayed gated since.
    """
    result = await forward({"type": "reset", "phase": "commit"})
    if result is None or result.status_code != 200:
        _logger.warning(
            "Runner did not confirm the context reset commit for %s (status %s)",
            session_id,
            None if result is None else result.status_code,
            extra={"session_id": session_id},
        )

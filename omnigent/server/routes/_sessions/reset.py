"""In-place context reset for a session (``POST /v1/sessions/{id}/reset``).

The session id, items and children stay; a ``context_reset`` boundary item
starts a new context epoch. Generic ``/compact`` is a reset that carries a
generated summary over the boundary. The runner checks and drops its caches
(``prepare``), the server persists the boundary, then the runner drops any
history cached meanwhile and releases the harness (``commit``), and the server
publishes ``session.reset``. A native session also loses its vendor session id,
so the relaunch starts a new vendor conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

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
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
)

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
#: Room ``_shorten_middle`` leaves for its note; fits any realistic character count.
_SHORTENED_NOTE_RESERVE = 40
_STOP_WAIT_S = 10.0
_STOP_POLL_S = 0.2
#: Forward budget for reset controls; ``commit`` releases the harness process, and
#: a codex ``prepare`` stops the app-server and reaps what survives it.
RESET_FORWARD_TIMEOUT_S = 50.0
#: A boundary's carried summary, matching ``ResetSummaryText.text``.
_RESET_SUMMARY_LIMIT = 64_000
#: Share of the model's context window the summariser input may fill.
_SUMMARY_INPUT_WINDOW_SHARE = 0.7
#: Recent turns kept verbatim by ``/compact --truncate``.
_TRUNCATE_TAIL_MESSAGES = 10
#: How long a compact POST waits to report a quick failure before it acks.
_COMPACT_ACK_GRACE_S = 10.0
#: ``/compact`` has no reset overrides, so its refusals only name what to wait for.
_COMPACT_RETRY = "then run /compact again"
#: Background compacts; the loop holds only weak references to tasks.
# custom-lint: disable-next=workspace-scoped-cache -- task objects are globally unique
_compact_tasks: set[asyncio.Task[None]] = set()

#: Native harnesses whose runner relaunches the terminal on a fresh vendor session.
IN_PLACE_RESET_NATIVE_HARNESSES = frozenset({"claude-native", "codex-native"})
#: Labels that make a native launch resume or rebuild the pre-reset transcript.
_NATIVE_RESUME_LABEL_KEYS = (FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY, FORK_CARRY_HISTORY_LABEL_KEY)

RunnerForward = Callable[[dict[str, Any]], Awaitable[Any]]


class _ChildLookup(Protocol):
    """The two store reads a reset uses to see sub-agent rows."""

    def list_child_conversation_ids_by_parent(
        self, parent_conversation_ids: list[str]
    ) -> dict[str, list[str]]:
        """Return direct child ids for each parent."""

    def get_conversations(self, conversation_ids: list[str]) -> Mapping[str, Conversation]:
        """Return the conversations that exist for ``conversation_ids``."""


@dataclass(frozen=True)
class MirroredChild:
    """A sub-agent row as stored. Its status can outlive the pane."""

    session_id: str
    status: str | None
    runner_id: str | None = None


@dataclass(frozen=True)
class RunningWork:
    """Sub-agents and background tasks a reset still has to account for."""

    subagents: int = 0
    background_tasks: int = 0


def count_live_running_work(
    session_id: str,
    children: Iterable[MirroredChild],
    *,
    observed_status: Mapping[str, str],
    runner_online: Mapping[str, bool],
    background_tasks: int,
) -> RunningWork:
    """Count work that blocks a reset.

    ``observed_status`` is the relay cache. ``runner_online`` is the live
    check: ``False`` means that pane's runner is gone, so a mirrored
    ``running`` row does not count. When the check cannot tell, a mirrored
    running row still counts, and ``acknowledge_running_work`` overrides it.

    :param session_id: Session being reset, e.g. ``"conv_parent"``.
    :param children: Sub-agent rows belonging to that session.
    :param observed_status: Relay status by session id. Absent ids have no
        fresh observation.
    :param runner_online: Live runner reachability by session id. Absent ids
        were not checked.
    :param background_tasks: Mirrored background-task tally for ``session_id``.
    :returns: Counts of work that is still live, or still unverified.
    """
    subagents = sum(
        1
        for child in children
        if _child_still_running(session_id, child, observed_status, runner_online)
    )
    background = background_tasks if background_tasks > 0 else 0
    if background and runner_online.get(session_id) is False:
        background = 0
    return RunningWork(subagents=subagents, background_tasks=background)


def running_work_message(work: RunningWork, *, compact: bool = False) -> str:
    """Return the refusal: the override for a reset, what to wait for on ``/compact``."""
    parts: list[str] = []
    if work.subagents:
        noun = "sub-agent" if work.subagents == 1 else "sub-agents"
        parts.append(f"{work.subagents} running {noun}")
    if work.background_tasks:
        noun = "background task" if work.background_tasks == 1 else "background tasks"
        parts.append(f"{work.background_tasks} {noun}")
    busy = " and ".join(parts)
    if compact:
        return f"The session has {busy}; wait for that work to finish, {_COMPACT_RETRY}"
    return f"The session has {busy}; retry with acknowledge_running_work=true"


def running_work_snapshot(
    session_id: str,
    conversation_store: _ChildLookup,
    *,
    observed_status: Callable[[str], str | None],
    runner_online_of: Callable[[list[str]], Mapping[str, bool]] | None,
    background_tasks: int,
) -> RunningWork:
    """Load sub-agent rows and count those the live check still sees.

    :param session_id: Session being reset.
    :param conversation_store: Store that lists child ids and their rows.
    :param observed_status: Relay status for one session id, or ``None``.
    :param runner_online_of: Live runner check for the ids a stale row might
        still be charging to. ``None`` when this server cannot check.
    :param background_tasks: Mirrored background-task tally for the session.
    :returns: The counts :func:`count_live_running_work` produces.
    """
    children = _load_mirrored_children(conversation_store, session_id)
    observed: dict[str, str] = {}
    probe: list[str] = []
    seen: set[str] = set()

    def add_probe(target_id: str) -> None:
        if target_id not in seen:
            seen.add(target_id)
            probe.append(target_id)

    if background_tasks > 0:
        add_probe(session_id)
    for child in children:
        status = observed_status(child.session_id)
        if isinstance(status, str):
            observed[child.session_id] = status
        if child.status in _BUSY_STATUSES or status in _BUSY_STATUSES:
            add_probe(child.session_id if child.runner_id else session_id)
    online: Mapping[str, bool] = {}
    if probe and runner_online_of is not None:
        online = runner_online_of(probe)
    return count_live_running_work(
        session_id,
        children,
        observed_status=observed,
        runner_online=online,
        background_tasks=background_tasks,
    )


def _pane_id(session_id: str, child: MirroredChild) -> str:
    """Return the session whose runner is the child's pane.

    A native mirror often has no runner of its own; its pane is the parent's.
    """
    return child.session_id if child.runner_id else session_id


def _child_still_running(
    session_id: str,
    child: MirroredChild,
    observed_status: Mapping[str, str],
    runner_online: Mapping[str, bool],
) -> bool:
    observed = observed_status.get(child.session_id)
    mirrored_busy = child.status in _BUSY_STATUSES
    observed_busy = observed in _BUSY_STATUSES
    if not mirrored_busy and not observed_busy:
        return False
    if runner_online.get(_pane_id(session_id, child)) is False:
        return False
    if observed is not None and not observed_busy:
        return False
    return True


def _load_mirrored_children(
    conversation_store: _ChildLookup, session_id: str
) -> list[MirroredChild]:
    descendant_ids: list[str] = []
    seen = {session_id}
    frontier = [session_id]
    while frontier:
        grouped = conversation_store.list_child_conversation_ids_by_parent(frontier)
        next_frontier: list[str] = []
        for parent_id in frontier:
            for child_id in grouped.get(parent_id, ()):
                if child_id in seen:
                    continue
                seen.add(child_id)
                descendant_ids.append(child_id)
                next_frontier.append(child_id)
        frontier = next_frontier
    if not descendant_ids:
        return []
    rows = conversation_store.get_conversations(descendant_ids)
    return [
        MirroredChild(session_id=row.id, status=row.live_status, runner_id=row.runner_id)
        for row in rows.values()
    ]


async def _no_running_work() -> RunningWork:
    return RunningWork()


def is_reset_in_flight(session_id: str) -> bool:
    """Return whether a context reset is in progress for ``session_id`` here."""
    return session_id in _reset_in_flight


def reset_stable_id(session_id: str, idempotency_key: str) -> str:
    """Return the boundary item's stable id for an idempotency key."""
    digest = hashlib.sha256(f"{session_id}:{idempotency_key}".encode()).hexdigest()
    return digest[:32]


def _unsupported(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _runner_refusal(body: str) -> tuple[str, str]:
    """Map a runner 409 body to an error code and the words the caller sees."""
    try:
        parsed = json.loads(body)
        code, detail = parsed.get("error"), parsed.get("detail")
    except (ValueError, AttributeError):
        code, detail = None, None
    known = (
        ErrorCode.PENDING_INPUTS,
        ErrorCode.RESET_IN_PROGRESS,
        ErrorCode.RESET_UNSUPPORTED,
        ErrorCode.SESSION_BUSY,
    )
    message = detail if isinstance(detail, str) and detail else "The session refused the reset."
    return (code if code in known else ErrorCode.RESET_IN_PROGRESS), message


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
    native_harness: str | None,
    publish: Callable[[dict[str, Any]], object],
    dispatching: Callable[[], bool] = lambda: False,
    created_by: str | None = None,
    running_work_of: Callable[[], Awaitable[RunningWork]] | None = None,
    generate_summary: Callable[[], Awaitable[str]] | None = None,
    on_gates_passed: Callable[[], None] | None = None,
    compact: bool = False,
) -> ResetSessionResponse:
    """
    Reset a session's model context in place.

    :param status_of: Returns the live status, e.g. ``"running"``.
    :param has_server_pending: Whether server-queued input exists.
    :param drop_server_pending: Discards server-queued input.
    :param interrupt: Stops the running turn.
    :param forward: POSTs a control event to the runner; ``None`` when unbound.
    :param wake: Relaunches a sleeping runner; ``True`` when one is bound after.
    :param native_harness: The native terminal harness driving the session,
        e.g. ``"claude-native"``, or ``None`` for an SDK harness.
    :param publish: Publishes an SSE payload on the session stream.
    :param dispatching: Whether a message dispatch is still in flight here.
    :param running_work_of: Live sub-agent and background-task counts.
        ``None`` counts nothing, for a caller that has no children to check.
    :param generate_summary: Writes the text for ``summary='auto'``, after the
        gates and before the runner is touched, so a failure changes nothing.
        ``None`` refuses ``auto``, for a harness that cannot replay a summary.
    :param on_gates_passed: Called once the session is known to be resettable.
    :param compact: The caller is ``/compact``, whose refusals can't name
        request fields the command does not have.
    :returns: The new epoch and boundary item id.
    :raises OmnigentError: 409 for a busy turn, pending input, live
        sub-agents or background work, an unsupported harness, or a
        concurrent reset; 503 when no runner is reachable.
    """
    if reset.summary == "auto" and generate_summary is None:
        raise _unsupported(
            "summary 'auto' is not supported for this session's harness; "
            "use 'none' or {'text': ...}"
        )
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
    if native_harness is not None and native_harness not in IN_PLACE_RESET_NATIVE_HARNESSES:
        raise OmnigentError(
            "Reset in place is SDK-only for now; native support is planned.",
            code=ErrorCode.RESET_UNSUPPORTED,
        )
    if native_harness is not None and reset.summary != "none":
        raise _unsupported(
            f"summaries are not supported for {native_harness} yet; use summary 'none'"
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
            native_harness=native_harness,
            running_work_of=running_work_of or _no_running_work,
            generate_summary=generate_summary,
            on_gates_passed=on_gates_passed,
            compact=compact,
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
    native_harness: str | None,
    running_work_of: Callable[[], Awaitable[RunningWork]],
    generate_summary: Callable[[], Awaitable[str]] | None,
    on_gates_passed: Callable[[], None] | None,
    compact: bool,
) -> ResetSessionResponse:
    # A message that passed the input gate before the reset registered may
    # still be on its way to the runner; let it land so the busy check sees it.
    await _wait_until(lambda: not dispatching(), "A message is still being delivered")
    await _ensure_idle(reset, status_of, interrupt, compact=compact)
    await _ensure_no_running_work(reset, running_work_of, compact=compact)
    await _settle_pending_inputs(
        reset, session_id, has_server_pending, drop_server_pending, compact=compact
    )
    if on_gates_passed is not None:
        on_gates_passed()
    summary = await _resolve_summary(reset, generate_summary)
    # A summary can take minutes. ``commit`` would kill a turn begun meanwhile,
    # and its items would land in neither the summary nor the next context.
    if status_of() in _BUSY_STATUSES:
        raise OmnigentError(
            "A turn started while the summary was being written; the conversation is "
            "unchanged. Retry when it finishes.",
            code=ErrorCode.SESSION_BUSY,
        )
    await _settle_pending_inputs(
        reset, session_id, has_server_pending, drop_server_pending, compact=compact
    )
    runner_reached = await _forward_reset(reset, forward, wake, conv, native_harness)
    # Ahead of the boundary, so a conflict leaves no epoch that resumes the old chat.
    if native_harness is not None and not await asyncio.to_thread(
        _clear_native_session_id, conversation_store, session_id
    ):
        if runner_reached:
            # ``prepare`` stopped the pane; relaunch it on the id that won.
            await _commit_reset(session_id, forward, native_harness)
        raise OmnigentError(
            "The session's native session id changed during the reset; retry.",
            code=ErrorCode.CONFLICT,
        )
    item = await asyncio.to_thread(
        conversation_store.append_context_reset,
        session_id,
        response_id=f"reset_{int(time.time() * 1000)}",
        summary=summary,
        reason="api",
        stable_id=stable_id,
        created_by=created_by,
    )
    if native_harness is not None:
        # Before ``commit``: the runner relaunches from this cleared state. An
        # unreached runner's local resume hint and labels left by a crash here
        # predate the new epoch, so the next launch ignores them.
        await asyncio.to_thread(_delete_native_resume_labels, conversation_store, session_id)
    if runner_reached:
        await _commit_reset(session_id, forward, native_harness)
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


async def _settle_pending_inputs(
    reset: ResetSessionRequest,
    session_id: str,
    has_server_pending: Callable[[], Awaitable[bool]],
    drop_server_pending: Callable[[], Awaitable[None]],
    *,
    compact: bool = False,
) -> None:
    """Refuse queued input, or drop it when the request said to."""
    if not (await has_server_pending() or pending_inputs.has_pending(session_id)):
        return
    if reset.pending == "reject":
        raise OmnigentError(
            f"The session has queued input; wait for it to be handled, {_COMPACT_RETRY}"
            if compact
            else "The session has queued input; retry with pending='drop'",
            code=ErrorCode.PENDING_INPUTS,
        )
    await drop_server_pending()
    while pending_inputs.resolve_oldest(session_id) is not None:
        pass


async def _resolve_summary(
    reset: ResetSessionRequest,
    generate_summary: Callable[[], Awaitable[str]] | None,
) -> str | None:
    """Return the text the boundary carries, generating it for ``summary='auto'``."""
    if isinstance(reset.summary, ResetSummaryText):
        return reset.summary.text
    if reset.summary != "auto" or generate_summary is None:
        return None
    summary = (await generate_summary()).strip()
    if not summary:
        raise OmnigentError(
            "Compaction failed: the summariser returned no text; the conversation "
            "is unchanged. Retry, or run /compact --truncate.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return summary[:_RESET_SUMMARY_LIMIT]


def _load_epoch_messages(
    conversation_store: ConversationStore, session_id: str
) -> list[dict[str, Any]]:
    """Return the current epoch as input items, or refuse when there is none."""
    from omnigent.runtime.prompt import history_to_input_items
    from omnigent.runtime.workflow import _load_initial_history

    messages = history_to_input_items(_load_initial_history(conversation_store, session_id).items)
    if not messages:
        raise OmnigentError("There is nothing to compact yet.", code=ErrorCode.INVALID_INPUT)
    return messages


def _cap_oldest_messages(
    messages: list[dict[str, Any]], *, model: str, token_limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Drop oldest messages until ``messages`` fit; return them and the drop count."""
    from omnigent.runtime.compaction import _pair_aware_drop_count, count_tokens

    try:
        token_counts = [count_tokens([message], model) for message in messages]
    except Exception:
        _logger.info("Falling back to a size estimate for the summariser input", exc_info=True)
        token_counts = [len(json.dumps(message, default=str)) // 4 for message in messages]
    remaining = sum(token_counts)
    start = 0
    while remaining > token_limit:
        drop_count = _pair_aware_drop_count(messages[start:])
        # The final message or tool pair stays, whatever its size.
        if start + drop_count >= len(messages):
            break
        remaining -= sum(token_counts[start : start + drop_count])
        start += drop_count
    return messages[start:], start


async def summarize_for_reset(
    *,
    session_id: str,
    conversation_store: ConversationStore,
    model: str,
    runner_client: Any | None,
) -> str:
    """
    Summarise the current epoch with the runner's credentials.

    :param model: The provider-prefixed model that writes the summary.
    :param runner_client: The client for the session's runner. The server's
        own credentials are never a fallback, so ``None`` is refused.
    :returns: The summary text.
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` without a runner; an
        actionable error when the summariser fails.
    """
    from omnigent.llms.context_window import get_model_context_window
    from omnigent.runtime.compaction import (
        _clear_binary_content,
        _clear_tool_results,
        summarize_history,
    )

    if runner_client is None:
        raise OmnigentError(
            "Can't summarise this session while its runner is offline. Reconnect the "
            "session (send a message to wake it), then run /compact again.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    messages = await asyncio.to_thread(_load_epoch_messages, conversation_store, session_id)
    last_index = len(messages)
    messages = _clear_binary_content(_clear_tool_results(messages, last_index), last_index)
    context_window = get_model_context_window(model)
    capped, omitted = await asyncio.to_thread(
        _cap_oldest_messages,
        messages,
        model=model,
        token_limit=int(context_window * _SUMMARY_INPUT_WINDOW_SHARE),
    )
    if omitted:
        if not any(_message_text(message) for message in capped):
            raise OmnigentError(
                "Compaction failed: only tool activity fits the summariser's input, so there "
                "is nothing to summarise. The conversation is unchanged. Run /compact "
                "--truncate, or set compaction.model in the agent spec to a model with a "
                "larger context window.",
                code=ErrorCode.INVALID_INPUT,
            )
        capped = [_omission_note(omitted), *capped]
    try:
        result = await summarize_history(
            capped, None, model, runner_client=runner_client, conversation_id=session_id
        )
    except OmnigentError:
        raise
    except Exception as exc:
        raise OmnigentError(
            f"Compaction failed: the summariser could not use {model} on this runner "
            f"({_failure_detail(exc)}). The conversation is unchanged. Set compaction.model "
            "in the agent spec to a model the runner has credentials for, or run "
            "/compact --truncate.",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    return str(result.get("text") or "")


def _failure_detail(exc: BaseException) -> str:
    """Name a failure; timeouts and cancellations stringify to nothing."""
    return str(exc) or type(exc).__name__


def _omission_note(omitted: int) -> dict[str, Any]:
    """Return the message that tells the summariser its input is incomplete."""
    return {
        "role": "user",
        "content": (
            f"[{omitted} earlier messages were omitted from this conversation to fit the "
            "summariser's input. Say so in the summary; do not guess what they contained.]"
        ),
    }


def _message_text(message: Mapping[str, Any]) -> str:
    """Return a message's visible text; tool items and binary blocks have none."""
    if "role" not in message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def truncation_summary(session_id: str, conversation_store: ConversationStore) -> str:
    """
    Build the carry-over for ``/compact --truncate``, which needs no LLM.

    A message that would fill the summary alone is shortened, so one huge paste
    can't push out the turns around it.

    :returns: The omission marker followed by the most recent text turns, within
        the boundary's summary limit.
    """
    messages = [
        (message["role"], text)
        for message in _load_epoch_messages(conversation_store, session_id)
        if (text := _message_text(message))
    ]
    budget = _RESET_SUMMARY_LIMIT - len(_omission_marker(len(messages))) - 2
    line_limit = budget // 2 - 2
    kept: list[str] = []
    for role, text in reversed(messages[-_TRUNCATE_TAIL_MESSAGES:]):
        line = _shorten_middle(f"{role}: {text}", line_limit)
        budget -= len(line) + 2
        if budget < 0:
            break
        kept.insert(0, line)
    return "\n\n".join([_omission_marker(len(messages) - len(kept)), *kept])


def _shorten_middle(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, keeping both ends and saying how much went."""
    if len(text) <= limit:
        return text
    kept = limit - _SHORTENED_NOTE_RESERVE
    head = kept // 2
    note = f"\n[{len(text) - kept} characters omitted]\n"
    return f"{text[:head]}{note}{text[head - kept :]}"


def _omission_marker(omitted: int) -> str:
    return f"[{omitted} earlier messages omitted to fit the context window]"


async def start_compact_reset(
    *,
    run_reset: Callable[[Callable[[], None]], Awaitable[object]],
    on_started: Callable[[], None],
    on_completed: Callable[[], None],
    on_failed: Callable[[], None],
    on_failed_after_ack: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """
    Run a compacting reset without tying its summary to the POST.

    A refusal at the gates raises here, as does a failure within a short grace
    so the caller sees why. Beyond that the POST acks, as it does for a native
    compact, and the outcome arrives through the compaction SSE events.

    :param run_reset: Runs the reset, given the gates-passed callback.
    :param on_started: Publishes that compaction began, once the gates pass.
    :param on_completed: Publishes success.
    :param on_failed: Publishes failure, for any failure after ``on_started``.
    :param on_failed_after_ack: Shows the user why, once the POST can no longer.
    """
    gates_passed = asyncio.Event()
    caller_waiting = True

    def _gates_passed() -> None:
        on_started()
        gates_passed.set()

    async def _run() -> None:
        try:
            await run_reset(_gates_passed)
        except BaseException as exc:
            if gates_passed.is_set():
                _logger.warning("Compacting reset failed", exc_info=True)
                on_failed()
                if on_failed_after_ack and not caller_waiting and isinstance(exc, Exception):
                    await on_failed_after_ack(_compact_failure_message(exc))
            raise
        on_completed()

    def _forget(finished: asyncio.Task[None]) -> None:
        _compact_tasks.discard(finished)
        if not finished.cancelled():
            finished.exception()  # Reported through the event when nobody awaits it.

    task = asyncio.create_task(_run())
    _compact_tasks.add(task)
    task.add_done_callback(_forget)
    gate_waiter = asyncio.create_task(gates_passed.wait())
    try:
        await asyncio.wait({task, gate_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            await asyncio.wait({task}, timeout=_COMPACT_ACK_GRACE_S)
    finally:
        gate_waiter.cancel()
        caller_waiting = False
    if task.done():
        task.result()


def _compact_failure_message(exc: Exception) -> str:
    """Return what the user reads about a compaction the POST already acked."""
    if isinstance(exc, OmnigentError):
        return exc.message
    return f"Compaction failed: {_failure_detail(exc)}"


def _clear_native_session_id(conversation_store: ConversationStore, session_id: str) -> bool:
    """Clear the vendor session id; ``False`` when another writer changed it.

    Read here rather than at the request's start: the pane is stopped by now,
    so a first hook that landed meanwhile is still the pre-reset session's id.
    """
    current = conversation_store.get_conversation(session_id)
    expected = current.external_session_id if current is not None else None
    return conversation_store.reset_external_session_id(session_id, expected=expected)


def _delete_native_resume_labels(conversation_store: ConversationStore, session_id: str) -> None:
    """Drop the fork labels so the relaunch does not rebuild the pre-reset chat."""
    for key in _NATIVE_RESUME_LABEL_KEYS:
        conversation_store.delete_label(session_id, key)


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
    *,
    compact: bool = False,
) -> None:
    if status_of() not in _BUSY_STATUSES:
        return
    if not reset.stop_running:
        raise OmnigentError(
            f"The session is running; wait for the turn to finish, {_COMPACT_RETRY}"
            if compact
            else "The session is running; retry with stop_running=true",
            code=ErrorCode.SESSION_BUSY,
        )
    await interrupt()
    await _wait_until(
        lambda: status_of() not in _BUSY_STATUSES, "The running turn did not stop in time"
    )


async def _ensure_no_running_work(
    reset: ResetSessionRequest,
    running_work_of: Callable[[], Awaitable[RunningWork]],
    *,
    compact: bool = False,
) -> None:
    """Refuse while sub-agents or background work are still live.

    The count comes from a live check. A mirrored row whose pane is gone
    does not count. ``acknowledge_running_work`` overrides a count the
    check still got wrong.
    """
    work = await running_work_of()
    if work.subagents == 0 and work.background_tasks == 0:
        return
    if reset.acknowledge_running_work:
        return
    raise OmnigentError(running_work_message(work, compact=compact), code=ErrorCode.SESSION_BUSY)


async def _forward_reset(
    reset: ResetSessionRequest,
    forward: RunnerForward,
    wake: Callable[[], Awaitable[bool]],
    conv: Conversation,
    native_harness: str | None = None,
) -> bool:
    """Ask the runner to check and drop its caches; ``False`` when none is bound."""
    event = _reset_event("prepare", native_harness) | {"pending": reset.pending}
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
        code, message = _runner_refusal(result.body)
        raise OmnigentError(message, code=code)
    raise OmnigentError(
        f"Reset failed: runner returned {result.status_code}", code=ErrorCode.INTERNAL_ERROR
    )


def _reset_event(phase: str, native_harness: str | None) -> dict[str, Any]:
    """Build a reset control; it names a native harness so the runner need not guess."""
    event: dict[str, Any] = {"type": "reset", "phase": phase}
    if native_harness is not None:
        event["harness"] = native_harness
    return event


async def _commit_reset(
    session_id: str, forward: RunnerForward, native_harness: str | None = None
) -> None:
    """Drop history a turn cached before the boundary landed, and release the harness.

    A failure here is logged, not raised: the caller's outcome is already settled,
    the ``prepare`` step dropped the caches and new input stayed gated since.
    """
    result = await forward(_reset_event("commit", native_harness))
    if result is None or result.status_code != 200:
        _logger.warning(
            "Runner did not confirm the context reset commit for %s (status %s)",
            session_id,
            None if result is None else result.status_code,
            extra={"session_id": session_id},
        )

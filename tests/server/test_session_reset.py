"""Unit tests for the in-place context reset gates and runner forwarding."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities import ContextResetData, Conversation, ConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions import reset as reset_module
from omnigent.server.routes._sessions.reset import (
    is_reset_in_flight,
    run_context_reset,
    running_work_snapshot,
)
from omnigent.server.schemas import ResetSessionRequest
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _Forwarded:
    status_code: int
    body: str = ""


class _Children:
    """Child rows a reset's live check reads, without a database."""

    def __init__(self, rows: list[Conversation]) -> None:
        self._rows = rows

    def list_child_conversation_ids_by_parent(
        self, parent_conversation_ids: list[str]
    ) -> dict[str, list[str]]:
        return {
            parent: [row.id for row in self._rows if row.parent_conversation_id == parent]
            for parent in parent_conversation_ids
        }

    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation]:
        wanted = set(conversation_ids)
        return {row.id: row for row in self._rows if row.id in wanted}


def _child(status: str, *, runner_id: str | None = None) -> Conversation:
    return Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        parent_conversation_id="conv_1",
        kind="sub_agent",
        live_status=status,
        runner_id=runner_id,
    )


@dataclass
class _Store:
    items: dict[str, ConversationItem] = field(default_factory=dict)
    epoch: int = 0
    #: Native resume-state writes and runner forwards, in call order.
    log: list[str] = field(default_factory=list)
    external_session_id: str | None = "sid-old"
    #: Written after the reset reads the id, ahead of its compare-and-clear.
    racing_external_session_id: str | None = None

    def get_conversation(self, conversation_id: str) -> Conversation:
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            external_session_id=self.external_session_id,
        )

    def reset_external_session_id(self, conversation_id: str, *, expected: str | None) -> bool:
        if self.racing_external_session_id is not None:
            self.external_session_id = self.racing_external_session_id
        if self.external_session_id != expected:
            self.log.append("keep external_session_id")
            return False
        self.external_session_id = None
        self.log.append("clear external_session_id")
        return True

    def delete_label(self, conversation_id: str, key: str) -> None:
        self.log.append(f"delete {key}")

    def get_item(self, conversation_id: str, item_id: str) -> ConversationItem | None:
        return self.items.get(item_id)

    def append_context_reset(
        self, conversation_id: str, *, stable_id: str | None = None, **kwargs: Any
    ) -> ConversationItem:
        self.epoch += 1
        item = ConversationItem(
            id=stable_id or f"reset_{self.epoch}",
            type="context_reset",
            status="completed",
            response_id=kwargs["response_id"],
            created_at=1,
            data=ContextResetData(epoch=self.epoch, summary=kwargs.get("summary")),
        )
        self.items[item.id] = item
        return item


@dataclass
class _Harness:
    status: str | None = "idle"
    server_pending: bool = False
    runner_results: list[_Forwarded | None] = field(
        default_factory=lambda: list[_Forwarded | None]([_Forwarded(200), _Forwarded(200)])
    )
    wake_result: bool = False
    native_harness: str | None = None
    runner_id: str | None = "runner_1"
    host_id: str | None = None
    store: _Store = field(default_factory=_Store)
    forwarded: list[dict[str, Any]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    dropped: bool = False
    interrupted: bool = False
    dispatching: bool = False
    children: list[Conversation] = field(default_factory=list)
    observed_status: dict[str, str] = field(default_factory=dict)
    runner_online: dict[str, bool] = field(default_factory=dict)
    background_tasks: int = 0
    items_seen_by_forward: list[int] = field(default_factory=list)
    forward_gate: asyncio.Event | None = None
    #: Recorded by a hook while the runner handles ``prepare``.
    id_landing_during_prepare: str | None = None

    async def run(self, **request: Any) -> Any:
        async def has_pending() -> bool:
            return self.server_pending

        async def drop() -> None:
            self.dropped = True

        async def interrupt() -> None:
            self.interrupted = True
            self.status = "idle"

        async def forward(event: dict[str, Any]) -> _Forwarded | None:
            self.forwarded.append(event)
            self.store.log.append(f"forward {event['phase']}")
            self.items_seen_by_forward.append(len(self.store.items))
            if event["phase"] == "prepare" and self.id_landing_during_prepare is not None:
                self.store.external_session_id = self.id_landing_during_prepare
            if self.forward_gate is not None:
                await self.forward_gate.wait()
            return self.runner_results.pop(0) if self.runner_results else None

        async def wake() -> bool:
            return self.wake_result

        async def running_work() -> Any:
            return running_work_snapshot(
                "conv_1",
                _Children(self.children),
                observed_status=lambda sid: self.observed_status.get(sid),
                runner_online_of=lambda ids: {
                    sid: self.runner_online[sid] for sid in ids if sid in self.runner_online
                },
                background_tasks=self.background_tasks,
            )

        return await run_context_reset(
            session_id="conv_1",
            conv=Conversation(
                id="conv_1",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_1",
                runner_id=self.runner_id,
                host_id=self.host_id,
            ),
            reset=ResetSessionRequest.model_validate(request),
            conversation_store=self.store,  # type: ignore[arg-type]
            status_of=lambda: self.status,
            has_server_pending=has_pending,
            drop_server_pending=drop,
            interrupt=interrupt,
            forward=forward,
            wake=wake,
            native_harness=self.native_harness,
            publish=self.published.append,
            dispatching=lambda: self.dispatching,
            running_work_of=running_work,
        )


_PREPARE = {"type": "reset", "phase": "prepare", "pending": "reject"}
_COMMIT = {"type": "reset", "phase": "commit"}


async def _error_code(harness: _Harness, **request: Any) -> str:
    with pytest.raises(OmnigentError) as caught:
        await harness.run(**request)
    return caught.value.code


async def test_reset_forwards_persists_and_publishes() -> None:
    harness = _Harness()

    result = await harness.run(summary={"text": "carried"})

    assert (result.epoch, result.boundary_item_id) == (1, "reset_1")
    assert harness.forwarded == [_PREPARE, _COMMIT]
    # The boundary is persisted between the runner's prepare and commit.
    assert harness.items_seen_by_forward == [0, 1]
    assert harness.published[0]["type"] == "session.reset"
    assert harness.published[0]["summary_present"] is True
    assert not is_reset_in_flight("conv_1")


async def test_idempotency_key_returns_the_first_boundary() -> None:
    harness = _Harness()

    first = await harness.run(idempotency_key="key-1")
    again = await harness.run(idempotency_key="key-1")

    assert again.boundary_item_id == first.boundary_item_id
    assert harness.forwarded == [_PREPARE, _COMMIT]


async def test_concurrent_retry_of_a_key_shares_the_first_result() -> None:
    harness = _Harness(forward_gate=asyncio.Event())

    first_task = asyncio.create_task(harness.run(idempotency_key="key-1"))
    while not harness.forwarded:
        await asyncio.sleep(0)
    again_task = asyncio.create_task(harness.run(idempotency_key="key-1"))
    await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- retry hops threads
    harness.forward_gate.set()
    first, again = await asyncio.gather(first_task, again_task)

    assert again == first
    assert harness.forwarded == [_PREPARE, _COMMIT]
    assert len(harness.store.items) == 1


async def test_reset_waits_for_an_in_flight_message_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reset_module, "_STOP_POLL_S", 0.01)
    harness = _Harness(dispatching=True)

    async def land_message() -> None:
        await asyncio.sleep(0.03)
        harness.dispatching = False
        harness.status = "running"

    landing = asyncio.create_task(land_message())
    assert await _error_code(harness) == ErrorCode.SESSION_BUSY
    await landing
    assert harness.forwarded == []


async def test_failed_commit_keeps_the_persisted_boundary() -> None:
    harness = _Harness(runner_results=[_Forwarded(200), None])

    result = await harness.run()

    assert result.epoch == 1
    assert harness.forwarded == [_PREPARE, _COMMIT]


@pytest.mark.parametrize(
    ("harness", "request_body", "code"),
    [
        (_Harness(status="running"), {}, ErrorCode.SESSION_BUSY),
        (_Harness(server_pending=True), {}, ErrorCode.PENDING_INPUTS),
        (_Harness(native_harness="pi-native"), {}, ErrorCode.RESET_UNSUPPORTED),
        (
            _Harness(native_harness="claude-native"),
            {"summary": {"text": "carried"}},
            ErrorCode.INVALID_INPUT,
        ),
        (
            _Harness(native_harness="codex-native"),
            {"summary": {"text": "carried"}},
            ErrorCode.INVALID_INPUT,
        ),
        (_Harness(runner_results=[_Forwarded(204)]), {}, ErrorCode.RESET_UNSUPPORTED),
        (
            _Harness(runner_results=[_Forwarded(409, '{"error": "pending_inputs"}')]),
            {},
            ErrorCode.PENDING_INPUTS,
        ),
        (
            _Harness(
                native_harness="claude-native",
                runner_results=[_Forwarded(409, '{"error": "reset_in_progress"}')],
            ),
            {},
            ErrorCode.RESET_IN_PROGRESS,
        ),
        (_Harness(runner_results=[None], host_id="host_1"), {}, ErrorCode.RUNNER_UNAVAILABLE),
        (_Harness(), {"summary": "auto"}, ErrorCode.INVALID_INPUT),
        (_Harness(), {"pending": "carry"}, ErrorCode.INVALID_INPUT),
    ],
)
async def test_reset_refusals_persist_nothing(
    harness: _Harness, request_body: dict[str, Any], code: str
) -> None:
    assert await _error_code(harness, **request_body) == code
    assert harness.store.items == {}
    assert harness.published == []
    assert not is_reset_in_flight("conv_1")


async def test_runner_refusal_reaches_the_caller_in_plain_words() -> None:
    body = '{"error": "reset_in_progress", "detail": "Claude is still starting."}'
    harness = _Harness(native_harness="claude-native", runner_results=[_Forwarded(409, body)])

    with pytest.raises(OmnigentError) as refused:
        await harness.run()

    assert refused.value.message == "Claude is still starting."


async def test_other_native_harnesses_keep_the_planned_refusal() -> None:
    with pytest.raises(OmnigentError) as caught:
        await _Harness(native_harness="pi-native").run()

    assert caught.value.code == ErrorCode.RESET_UNSUPPORTED
    assert "native support is planned" in str(caught.value)


@pytest.mark.parametrize("native_harness", ["claude-native", "codex-native"])
async def test_claude_native_reset_clears_resume_state_before_the_relaunch(
    native_harness: str,
) -> None:
    harness = _Harness(native_harness=native_harness)

    result = await harness.run()

    assert result.epoch == 1
    # The runner's commit relaunches the vendor, so it must find no id to resume.
    assert harness.store.log == [
        "forward prepare",
        "clear external_session_id",
        f"delete {FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY}",
        f"delete {FORK_CARRY_HISTORY_LABEL_KEY}",
        "forward commit",
    ]
    # Both phases name the harness, so a runner without the spec cached still
    # takes the native path.
    assert harness.forwarded == [
        {**_PREPARE, "harness": native_harness},
        {**_COMMIT, "harness": native_harness},
    ]
    assert harness.published[0]["type"] == "session.reset"


@pytest.mark.parametrize("native_harness", ["claude-native", "codex-native"])
async def test_claude_native_reset_without_a_bound_runner_still_clears_resume_state(
    native_harness: str,
) -> None:
    # The epoch the boundary raises makes the next launch skip the stale local hint.
    harness = _Harness(
        native_harness=native_harness, runner_id=None, host_id="host_1", runner_results=[None]
    )

    result = await harness.run()

    assert result.epoch == 1
    assert harness.store.log == [
        "forward prepare",
        "clear external_session_id",
        f"delete {FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY}",
        f"delete {FORK_CARRY_HISTORY_LABEL_KEY}",
    ]


@pytest.mark.parametrize("native_harness", ["claude-native", "codex-native"])
async def test_claude_native_reset_clears_an_id_recorded_during_prepare(
    native_harness: str,
) -> None:
    harness = _Harness(native_harness=native_harness)
    harness.store.external_session_id = None
    harness.id_landing_during_prepare = "sid-from-the-stopped-pane"

    await harness.run()

    assert harness.store.external_session_id is None


@pytest.mark.parametrize("native_harness", ["claude-native", "codex-native"])
@pytest.mark.parametrize(
    ("bound", "expected_log"),
    [
        pytest.param(
            True,
            ["forward prepare", "keep external_session_id", "forward commit"],
            id="runner-relaunches-on-the-winning-id",
        ),
        pytest.param(False, ["forward prepare", "keep external_session_id"], id="no-runner"),
    ],
)
async def test_claude_native_reset_conflict_persists_no_boundary(
    native_harness: str, bound: bool, expected_log: list[str]
) -> None:
    harness = _Harness(
        native_harness=native_harness,
        runner_id="runner_1" if bound else None,
        host_id=None if bound else "host_1",
        runner_results=[_Forwarded(200), _Forwarded(200)] if bound else [None],
        store=_Store(racing_external_session_id="sid-winner"),
    )

    assert await _error_code(harness) == ErrorCode.CONFLICT

    assert harness.store.log == expected_log
    assert harness.store.external_session_id == "sid-winner"
    assert harness.store.items == {}
    assert harness.published == []
    assert not is_reset_in_flight("conv_1")


async def test_sdk_reset_keeps_the_external_session_id() -> None:
    harness = _Harness()

    await harness.run()

    assert harness.store.log == ["forward prepare", "forward commit"]
    assert harness.store.external_session_id == "sid-old"


_LIVE_CHILD = "The session has 1 running sub-agent; retry with acknowledge_running_work=true"


def _live_child() -> _Harness:
    # No runner of its own: the pane is the parent's, and the live check says it is up.
    return _Harness(
        children=[_child("running")],
        observed_status={"child": "running"},
        runner_online={"conv_1": True},
    )


async def test_live_child_blocks_reset() -> None:
    harness = _live_child()

    with pytest.raises(OmnigentError) as caught:
        await harness.run()

    assert caught.value.code == ErrorCode.SESSION_BUSY
    assert caught.value.message == _LIVE_CHILD
    assert harness.store.items == {}
    assert harness.forwarded == []


async def test_acknowledged_running_child_resets() -> None:
    result = await _live_child().run(acknowledge_running_work=True)

    assert result.epoch == 1


async def test_dead_mirrored_child_does_not_block() -> None:
    # The row and the relay cache still say running after the pane's runner died.
    harness = _Harness(
        children=[_child("running")],
        observed_status={"child": "running"},
        runner_online={"conv_1": False},
    )

    result = await harness.run()

    assert result.epoch == 1
    assert harness.forwarded[0]["phase"] == "prepare"


async def test_live_background_work_blocks_until_acknowledged() -> None:
    harness = _Harness(background_tasks=2, runner_online={"conv_1": True})

    with pytest.raises(OmnigentError) as caught:
        await harness.run()

    assert caught.value.message == (
        "The session has 2 background tasks; retry with acknowledge_running_work=true"
    )
    assert harness.forwarded == []

    result = await harness.run(acknowledge_running_work=True)

    assert result.epoch == 1


async def test_dead_pane_drops_a_stale_background_tally() -> None:
    harness = _Harness(background_tasks=2, runner_online={"conv_1": False})

    result = await harness.run()

    assert result.epoch == 1


async def test_stop_running_interrupts_first() -> None:
    harness = _Harness(status="running")

    await harness.run(stop_running=True)

    assert harness.interrupted is True
    assert len(harness.store.items) == 1


async def test_pending_drop_discards_queued_input() -> None:
    harness = _Harness(server_pending=True)

    await harness.run(pending="drop")

    assert harness.dropped is True
    assert harness.forwarded == [{**_PREPARE, "pending": "drop"}, _COMMIT]


async def test_unbound_session_resets_without_a_runner() -> None:
    harness = _Harness(runner_id=None, runner_results=[None])

    result = await harness.run()

    assert result.epoch == 1
    assert harness.forwarded == [_PREPARE]


async def test_wake_retries_the_forward() -> None:
    harness = _Harness(
        runner_results=[None, _Forwarded(200), _Forwarded(200)], host_id="host_1", wake_result=True
    )

    await harness.run()

    assert harness.forwarded == [_PREPARE, _PREPARE, _COMMIT]


async def test_concurrent_reset_is_refused() -> None:
    reset_module._reset_in_flight["conv_1"] = reset_module._InFlightReset(stable_id=None)
    try:
        assert (
            await _error_code(_Harness(), idempotency_key="key-2") == ErrorCode.RESET_IN_PROGRESS
        )
    finally:
        reset_module._reset_in_flight.pop("conv_1", None)

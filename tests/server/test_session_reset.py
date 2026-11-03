"""Unit tests for the in-place context reset gates and runner forwarding."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.entities import ContextResetData, Conversation, ConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions import reset as reset_module
from omnigent.server.routes._sessions.reset import is_reset_in_flight, run_context_reset
from omnigent.server.schemas import ResetSessionRequest

pytestmark = pytest.mark.asyncio


@dataclass
class _Forwarded:
    status_code: int
    body: str = ""


@dataclass
class _Store:
    items: dict[str, ConversationItem] = field(default_factory=dict)
    epoch: int = 0

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
    native: bool = False
    runner_id: str | None = "runner_1"
    host_id: str | None = None
    store: _Store = field(default_factory=_Store)
    forwarded: list[dict[str, Any]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    dropped: bool = False
    interrupted: bool = False
    dispatching: bool = False
    items_seen_by_forward: list[int] = field(default_factory=list)
    forward_gate: asyncio.Event | None = None

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
            self.items_seen_by_forward.append(len(self.store.items))
            if self.forward_gate is not None:
                await self.forward_gate.wait()
            return self.runner_results.pop(0) if self.runner_results else None

        async def wake() -> bool:
            return self.wake_result

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
            native=self.native,
            publish=self.published.append,
            dispatching=lambda: self.dispatching,
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
        (_Harness(native=True), {}, ErrorCode.RESET_UNSUPPORTED),
        (_Harness(runner_results=[_Forwarded(204)]), {}, ErrorCode.RESET_UNSUPPORTED),
        (
            _Harness(runner_results=[_Forwarded(409, '{"error": "pending_inputs"}')]),
            {},
            ErrorCode.PENDING_INPUTS,
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

"""Unit coverage for the helpers behind generic ``/compact``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import compaction
from omnigent.server.routes._sessions import reset as reset_module
from omnigent.server.schemas import ResetSessionRequest
from omnigent.spec.parser import _parse_compaction


@pytest.fixture(autouse=True)
def _offline_token_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count by size, so these tests never fetch a tiktoken encoding."""
    monkeypatch.setattr(
        compaction, "count_tokens", lambda messages, model: len(str(messages)) // 4
    )


def _text_items(count: int, size: int = 1) -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"{index}:" + "x" * size} for index in range(count)]


def _patch_history(monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(reset_module, "_load_epoch_messages", lambda store, session: messages)


def test_cap_keeps_all_messages_that_fit() -> None:
    messages = _text_items(3)

    kept, omitted = reset_module._cap_oldest_messages(
        messages, model="openai/gpt-4o", token_limit=10_000
    )

    assert (kept, omitted) == (messages, 0)


def test_cap_drops_oldest_first_and_keeps_tool_pairs_whole() -> None:
    call = {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "c1", "output": "ok"}
    tail = {"role": "user", "content": "newest"}

    kept, omitted = reset_module._cap_oldest_messages(
        [call, output, tail],
        model="openai/gpt-4o",
        token_limit=compaction.count_tokens([tail], "openai/gpt-4o"),
    )

    assert kept == [tail]
    assert omitted == 2


def _tool_pair(size: int = 4_000) -> list[dict[str, Any]]:
    call = {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "x" * size}
    return [call, {"type": "function_call_output", "call_id": "c1", "output": "ok"}]


def test_cap_keeps_a_final_tool_pair_larger_than_the_limit() -> None:
    pair = _tool_pair()

    kept, omitted = reset_module._cap_oldest_messages(pair, model="openai/gpt-4o", token_limit=1)

    assert (kept, omitted) == (pair, 0)


def test_cap_stops_at_the_final_tool_pair_after_dropping_older_messages() -> None:
    pair = _tool_pair()

    kept, omitted = reset_module._cap_oldest_messages(
        [*_text_items(2, size=400), *pair], model="openai/gpt-4o", token_limit=1
    )

    assert (kept, omitted) == (pair, 2)


async def test_a_tail_with_no_text_is_refused_before_the_summariser_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unreachable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the summariser must not run on an empty tail")

    _patch_history(monkeypatch, [*_text_items(2, size=400), *_tool_pair()])
    monkeypatch.setattr(compaction, "summarize_history", unreachable)
    monkeypatch.setattr(reset_module, "_SUMMARY_INPUT_WINDOW_SHARE", 0.0)

    with pytest.raises(OmnigentError) as caught:
        await reset_module.summarize_for_reset(
            session_id="conv_1",
            conversation_store=object(),  # type: ignore[arg-type]
            model="openai/gpt-4o",
            runner_client=object(),
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert "unchanged" in caught.value.message
    assert "/compact --truncate" in caught.value.message
    assert "compaction.model" in caught.value.message


async def _refusal(
    *,
    compact: bool,
    status: str | None = None,
    work: reset_module.RunningWork | None = None,
    queued: bool = False,
) -> str:
    """Return the words a gate refuses with; anything past the gates fails the test."""

    async def has_queued() -> bool:
        return queued

    async def running_work() -> reset_module.RunningWork:
        return work or reset_module.RunningWork()

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused reset must not reach this")

    with pytest.raises(OmnigentError) as caught:
        await reset_module.run_context_reset(
            session_id="conv_refused",
            conv=object(),  # type: ignore[arg-type]
            reset=ResetSessionRequest(summary="auto"),
            conversation_store=object(),  # type: ignore[arg-type]
            status_of=lambda: status,
            has_server_pending=has_queued,
            drop_server_pending=unreachable,
            interrupt=unreachable,
            forward=unreachable,
            wake=unreachable,
            native_harness=None,
            publish=lambda payload: None,
            running_work_of=running_work,
            generate_summary=unreachable,
            compact=compact,
        )
    return caught.value.message


_REFUSAL_CASES = [
    pytest.param(
        {"status": "running"},
        "The session is running; retry with stop_running=true",
        id="running_turn",
    ),
    pytest.param(
        {"work": reset_module.RunningWork(subagents=2)},
        "The session has 2 running sub-agents; retry with acknowledge_running_work=true",
        id="sub_agents",
    ),
    pytest.param(
        {"queued": True},
        "The session has queued input; retry with pending='drop'",
        id="queued_input",
    ),
]


@pytest.mark.parametrize(("state", "reset_wording"), _REFUSAL_CASES)
async def test_reset_refusals_name_their_override(
    state: dict[str, Any], reset_wording: str
) -> None:
    assert await _refusal(compact=False, **state) == reset_wording


@pytest.mark.parametrize(("state", "reset_wording"), _REFUSAL_CASES)
async def test_compact_refusals_say_what_to_wait_for(
    state: dict[str, Any], reset_wording: str
) -> None:
    message = await _refusal(compact=True, **state)

    assert "wait" in message
    assert "then run /compact again" in message
    assert "retry" not in message
    assert "=" not in message


async def test_summary_without_a_runner_is_refused_before_loading() -> None:
    with pytest.raises(OmnigentError) as caught:
        await reset_module.summarize_for_reset(
            session_id="conv_1",
            conversation_store=object(),  # type: ignore[arg-type]
            model="openai/gpt-4o",
            runner_client=None,
        )

    assert caught.value.code == ErrorCode.RUNNER_UNAVAILABLE


async def test_capped_input_states_the_omission_count(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_summarize(
        messages: list[dict[str, Any]], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        seen["messages"] = messages
        return {"text": "the summary", "token_count": 3}

    _patch_history(monkeypatch, _text_items(6, size=400))
    monkeypatch.setattr(compaction, "summarize_history", fake_summarize)
    monkeypatch.setattr(reset_module, "_SUMMARY_INPUT_WINDOW_SHARE", 0.0)

    summary = await reset_module.summarize_for_reset(
        session_id="conv_1",
        conversation_store=object(),  # type: ignore[arg-type]
        model="openai/gpt-4o",
        runner_client=object(),
    )

    assert summary == "the summary"
    assert "5 earlier messages were omitted" in seen["messages"][0]["content"]
    assert len(seen["messages"]) == 2


async def test_summariser_failure_names_a_way_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_summarize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("401 no credentials")

    _patch_history(monkeypatch, _text_items(2))
    monkeypatch.setattr(compaction, "summarize_history", failing_summarize)

    with pytest.raises(OmnigentError) as caught:
        await reset_module.summarize_for_reset(
            session_id="conv_1",
            conversation_store=object(),  # type: ignore[arg-type]
            model="openai/gpt-4o",
            runner_client=object(),
        )

    assert "compaction.model" in str(caught.value)
    assert "--truncate" in str(caught.value)


def test_truncation_marks_the_omitted_count_and_keeps_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_history(
        monkeypatch,
        [*_text_items(12), {"type": "function_call", "call_id": "c", "name": "t"}],
    )

    summary = reset_module.truncation_summary("conv_1", object())  # type: ignore[arg-type]

    assert summary.startswith("[2 earlier messages omitted to fit the context window]")
    assert "user: 11:x" in summary
    assert "user: 1:x" not in summary


def test_truncation_stays_within_the_boundary_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_history(monkeypatch, _text_items(4, size=30_000))

    summary = reset_module.truncation_summary("conv_1", object())  # type: ignore[arg-type]

    assert len(summary) <= 64_000
    assert summary.startswith("[2 earlier messages omitted to fit the context window]")


class _Events:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_started": lambda: self.seen.append("started"),
            "on_completed": lambda: self.seen.append("completed"),
            "on_failed": lambda: self.seen.append("failed"),
        }


async def test_refusal_at_the_gates_raises_and_publishes_nothing() -> None:
    events = _Events()

    async def refused(gates_passed: Any) -> None:
        raise OmnigentError("busy", code=ErrorCode.SESSION_BUSY)

    with pytest.raises(OmnigentError):
        await reset_module.start_compact_reset(run_reset=refused, **events.callbacks())

    assert events.seen == []


async def test_quick_failure_after_the_gates_reaches_the_caller_and_the_stream() -> None:
    events = _Events()

    async def failing(gates_passed: Any) -> None:
        gates_passed()
        raise OmnigentError("summariser down", code=ErrorCode.INTERNAL_ERROR)

    with pytest.raises(OmnigentError):
        await reset_module.start_compact_reset(run_reset=failing, **events.callbacks())

    assert events.seen == ["started", "failed"]


async def test_slow_summary_acks_and_reports_through_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reset_module, "_COMPACT_ACK_GRACE_S", 0.0)
    events = _Events()
    release = asyncio.Event()

    async def slow(gates_passed: Any) -> None:
        gates_passed()
        await release.wait()
        raise OmnigentError("summariser down", code=ErrorCode.INTERNAL_ERROR)

    await reset_module.start_compact_reset(run_reset=slow, **events.callbacks())
    assert events.seen == ["started"]

    release.set()
    await asyncio.gather(*reset_module._compact_tasks, return_exceptions=True)
    assert events.seen == ["started", "failed"]


def test_compaction_model_is_optional_and_validated() -> None:
    assert _parse_compaction({}).model is None  # type: ignore[union-attr]
    parsed = _parse_compaction({"model": " anthropic/claude-haiku-4-5 "})
    assert parsed.model == "anthropic/claude-haiku-4-5"  # type: ignore[union-attr]
    with pytest.raises(OmnigentError):
        _parse_compaction({"model": 5})

"""``compaction_mode`` parity: the capability table against what really runs.

A harness declares ``reset_replay`` only with a proof here that a relaunched
executor's first prompt carries the summary and no pre-boundary text. A reset
kills the harness process, so the next turn runs on a rebuilt executor and each
proof starts from a fresh instance and the history loaded after the boundary.
Each proof reads the prompt off ``run_turn``'s wire output, so the executor's
own new-session decision stays under test.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnigent.harness_capabilities import CompactionMode
from omnigent.harness_plugins import compaction_mode_for, harness_capabilities
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.codex_executor import CodexExecutor, _CodexAppServerSession
from omnigent.inner.copilot_executor import CopilotExecutor
from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.inner.pi_executor import PiExecutor, _PiRpcSession, _PiSessionState
from omnigent.inner.qwen_executor import QwenExecutor
from omnigent.runner.app import NATIVE_COMPACT_HANDLERS
from omnigent.runner.native_controls import NativeControls
from omnigent.runner.session_history import split_history_at_boundary
from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages
from tests.inner.test_acp_executor import _FAKE_ACP_AGENT
from tests.inner.test_codex_executor import _FakeProcess as _FakeCodexProcess
from tests.inner.test_copilot_executor import _install_fake_copilot
from tests.inner.test_cursor_executor import _install_fake_sdk
from tests.inner.test_pi_executor import _FakeProcess

_PRE_BOUNDARY = "PRE-BOUNDARY-ORCHID"
_SUMMARY = "SUMMARY-CARRIED-OVER"
_NEXT_TURN = "NEXT-TURN-QUESTION"


def _message(role: str, text: str) -> dict[str, Any]:
    block = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": block, "text": text}]}


def _messages_after_reset(executor: Any) -> list[Any]:
    """Return what the adapter hands ``run_turn`` on the turn after a reset."""
    prefix, remaining = split_history_at_boundary(
        [
            _message("user", _PRE_BOUNDARY),
            _message("assistant", f"noted {_PRE_BOUNDARY}"),
            {"type": "context_reset", "summary": _SUMMARY},
        ]
    )
    return _translate_input_to_messages(
        [*prefix, *remaining, _message("user", _NEXT_TURN)],
        include_tool_history=executor.replays_tool_history(),
        preserve_content_blocks=executor.preserves_message_content_blocks(),
    )


async def _jsonrpc_first_prompt(executor: Any) -> str:
    """Drive an ACP-subprocess executor and capture its ``session/prompt`` text."""
    executor._initialized = True
    executor._session_id = "fresh"
    executor._proc = MagicMock()
    executor._proc.returncode = None
    loop = asyncio.get_running_loop()
    prompts: list[str] = []

    async def fake_send(message: dict[str, Any]) -> None:
        if message.get("method") != "session/prompt":
            return
        prompts.append(message["params"]["prompt"][0]["text"])
        request_id = message["id"]

        def _resolve() -> None:
            pending = executor._pending.get(request_id)
            if pending is not None and not pending.done():
                pending.set_result(
                    {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}}
                )

        loop.call_soon(_resolve)

    executor._send = fake_send
    async for _ in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
        pass
    return prompts[0]


async def _goose_prompt(_: Path, __: pytest.MonkeyPatch) -> str:
    return await _jsonrpc_first_prompt(GooseExecutor())


async def _qwen_prompt(_: Path, __: pytest.MonkeyPatch) -> str:
    return await _jsonrpc_first_prompt(QwenExecutor())


async def _acp_prompt(tmp_path: Path, _: pytest.MonkeyPatch) -> str:
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])
    executor = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))
    real_send = executor._send
    prompts: list[str] = []

    async def spying_send(message: dict[str, Any]) -> None:
        if message.get("method") == "session/prompt":
            prompts.append(message["params"]["prompt"][0]["text"])
        await real_send(message)

    executor._send = spying_send  # type: ignore[method-assign]
    try:
        async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
            pass
    finally:
        await executor.close()
    return prompts[0]


async def _copilot_prompt(_: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    state = _install_fake_copilot(monkeypatch, [[]])
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_x")
    executor = CopilotExecutor()
    async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
        pass
    return str(state["sent"][0])


async def _cursor_prompt(_: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    state = _install_fake_sdk(monkeypatch, [{"messages": [], "status": "finished", "result": ""}])
    executor = CursorExecutor(api_key="crsr_x")
    try:
        async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
            pass
    finally:
        await executor.close()
    return str(state["sent"][0])


async def _pi_prompt(_: Path, __: pytest.MonkeyPatch) -> str:
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor()
    rpc = _PiRpcSession()
    rpc._line_queue = asyncio.Queue()
    rpc.process = _FakeProcess()
    rpc._stderr_lines = []
    rpc._line_queue.put_nowait('{"type": "agent_end", "messages": []}')
    sent: list[dict[str, Any]] = []

    async def fake_send_command(command: dict[str, Any]) -> None:
        sent.append(command)

    async def fake_ensure_rpc(session_key: str, *args: Any, **kwargs: Any) -> _PiRpcSession:
        executor._session_states[session_key] = _PiSessionState(rpc=rpc)
        return rpc

    rpc.send_command = fake_send_command  # type: ignore[method-assign]
    executor._ensure_rpc = fake_ensure_rpc  # type: ignore[method-assign]
    async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
        pass
    return next(str(command["message"]) for command in sent if command.get("type") == "prompt")


class _ScriptedCodexAppServer(_CodexAppServerSession):
    """The real session with its app-server replaced at the JSON-RPC seam."""

    def __init__(self) -> None:
        super().__init__(codex_path="/bin/echo", cwd=".", env={}, tool_executor=None)
        self._proc = _FakeCodexProcess()
        self.thread_starts = 0
        self.turn_inputs: list[Any] = []

    async def start(self) -> None:
        pass

    async def _request(
        self, method: str, params: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        if method == "thread/start":
            self.thread_starts += 1
            return {"result": {"thread": {"id": f"thread-{self.thread_starts}"}}}
        if method != "turn/start":
            return {"result": {}}
        self.turn_inputs.append(params["input"])
        completed = {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
        asyncio.get_running_loop().call_later(0.01, self._events.put_nowait, completed)
        return {"result": {"turn": {"id": "turn-1"}}}


def _codex_executor() -> tuple[CodexExecutor, _ScriptedCodexAppServer]:
    server = _ScriptedCodexAppServer()
    with patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"):
        executor = CodexExecutor(app_session_factory=lambda **kwargs: server)
    return executor, server


async def _codex_prompt(_: Path, __: pytest.MonkeyPatch) -> str:
    executor, server = _codex_executor()
    async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
        pass
    assert server.thread_starts == 1
    return str(server.turn_inputs[0])


async def test_codex_turn_on_a_live_thread_replays_nothing() -> None:
    """Without the release a reset relies on, the summary never reaches Codex."""
    executor, server = _codex_executor()
    async for _event in executor.run_turn([{"role": "user", "content": _PRE_BOUNDARY}], [], "SYS"):
        pass

    async for _event in executor.run_turn(_messages_after_reset(executor), [], "SYS"):
        pass

    assert server.thread_starts == 1
    assert _SUMMARY not in str(server.turn_inputs[1])
    assert _NEXT_TURN in str(server.turn_inputs[1])


_FIRST_PROMPT: dict[str, Callable[[Path, pytest.MonkeyPatch], Awaitable[str]]] = {
    "acp": _acp_prompt,
    "codex": _codex_prompt,
    "copilot": _copilot_prompt,
    "cursor": _cursor_prompt,
    "goose": _goose_prompt,
    # Grok and jcode are configurations of the same executor as ``acp``.
    "grok": _acp_prompt,
    "jcode": _acp_prompt,
    "pi": _pi_prompt,
    "qwen": _qwen_prompt,
}


def _harnesses_in_mode(mode: CompactionMode) -> set[str]:
    return {
        harness
        for harness, capabilities in harness_capabilities().items()
        if capabilities.compaction_mode is mode
    }


def test_native_mode_matches_the_runner_dispatch_table() -> None:
    assert _harnesses_in_mode(CompactionMode.NATIVE) == set(NATIVE_COMPACT_HANDLERS)


def test_every_dispatch_entry_names_a_native_control() -> None:
    fields = set(NativeControls.__dataclass_fields__)
    assert set(NATIVE_COMPACT_HANDLERS.values()) <= fields


def test_every_reset_replay_harness_has_a_prompt_proof() -> None:
    assert _harnesses_in_mode(CompactionMode.RESET_REPLAY) == set(_FIRST_PROMPT)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", sorted(_FIRST_PROMPT))
async def test_first_prompt_after_reset_carries_only_the_summary(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = await _FIRST_PROMPT[harness](tmp_path, monkeypatch)

    assert _SUMMARY in prompt
    assert _NEXT_TURN in prompt
    assert _PRE_BOUNDARY not in prompt


@pytest.mark.parametrize(
    ("harness", "mode"),
    [
        ("claude", CompactionMode.NATIVE),
        ("acp:grok", CompactionMode.RESET_REPLAY),
        ("kimi", CompactionMode.NONE),
        ("no-such-harness", None),
        (None, None),
    ],
)
def test_compaction_mode_for_resolves_aliases_and_namespaces(
    harness: str | None, mode: CompactionMode | None
) -> None:
    assert compaction_mode_for(harness) is mode

"""Tests for the runner's claude-native base-args assembly.

``_build_claude_native_base_args`` is the pure seam that turns a
session's persisted launch config (reasoning_effort, model_override,
terminal_launch_args) into the base ``claude`` CLI args a
daemon/server-spawned runner launches with — before
``augment_claude_args`` layers on the bridge/MCP/hook/AP wiring. The
invariants under test (order, model precedence, ignore-unknown-effort)
are what make a host-spawned launch match what the CLI would have
passed. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from omnigent.errors import OmnigentError
from omnigent.harnesses.claude_native.main import (
    ClaudeNativeUcodeConfig,
    build_native_claude_terminal_env,
)
from omnigent.runner import pending_approvals
from omnigent.runner.app import _build_claude_native_base_args, _claude_terminal_env_unset
from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _CLAUDE_LAUNCH_ACCESS_REFUSED_MESSAGE,
    _CLAUDE_LAUNCH_REFUSED_MESSAGE,
    _CLAUDE_LAUNCH_STOPPED_CLOSED_MESSAGE,
    _CLAUDE_LAUNCH_STOPPED_HARNESS_MESSAGE,
    _CLAUDE_LAUNCH_STOPPED_UNAVAILABLE_MESSAGE,
    _ROUTED_SPAWN_ALLOWED_TOOLS,
    NativeLaunchContext,
    _cancel_claude_launch_redrive,
    _claude_launch_metadata_from_envelope,
    _claude_launch_permission_mode,
    _claude_launch_redrives,
    _claude_redrive_attempt,
    _claude_refused_status_sessions,
    _ClaudeLaunchRefusedError,
    _launch_claude,
    _load_claude_launch_metadata,
    _load_legacy_claude_launch_metadata,
    _native_terminal_start_error_payload,
    _publish_native_terminal_start_error,
    _redrive_refused_claude_launch,
    _routed_spawn_launch_args,
)
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY


@pytest.mark.parametrize(
    ("reasoning_effort", "model_override", "terminal_launch_args", "expected"),
    [
        # Effort only → "--effort <value>"; nothing else contributed.
        ("high", None, None, ("--effort", "high")),
        # Pass-through flags are included verbatim; model_override is
        # appended as a default --model because the user gave no --model.
        (
            None,
            "claude-opus-4-7",
            ["--dangerously-skip-permissions"],
            ("--dangerously-skip-permissions", "--model", "claude-opus-4-7"),
        ),
        # Explicit --model in pass-through args WINS over model_override
        # (space form): the override default must not be appended.
        (None, "claude-opus-4-7", ["--model", "sonnet"], ("--model", "sonnet")),
        # Explicit --model in pass-through args WINS (joined form): the
        # ``--model=X`` spelling must also suppress the override default.
        (None, "claude-opus-4-7", ["--model=sonnet"], ("--model=sonnet",)),
        # A pass-through --effort (either form) is the one Claude runs at,
        # so the persisted effort must not add a second, conflicting flag.
        ("high", None, ["--effort", "xhigh"], ("--effort", "xhigh")),
        ("high", None, ["--verbose", "--effort=max"], ("--verbose", "--effort=max")),
        # Full ordering: effort prefix, then pass-through, then the
        # model default last. A different order would mean the assembly
        # logic changed and the launch command no longer matches the CLI.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            ("--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # Nothing persisted → no args (Claude uses its settings.json
        # defaults). A non-empty result here would mean we injected a
        # phantom flag.
        (None, None, None, ()),
        # An empty pass-through list behaves like None — contributes
        # nothing, but the model default still applies.
        (None, "claude-opus-4-7", [], ("--model", "claude-opus-4-7")),
        # An unrecognised effort is dropped (not a Claude effort), so it
        # never reaches the CLI as a bogus ``--effort`` value.
        ("bogus-effort", None, None, ()),
        # Older clients put the prompt first; fresh launches still accept it
        # ahead of variadic flag values and the model default.
        (
            None,
            "claude-opus-4-7",
            ["hello", "--mcp-config", "mcp.json"],
            ("hello", "--mcp-config", "mcp.json", "--model", "claude-opus-4-7"),
        ),
    ],
    ids=[
        "effort-only",
        "model-default-appended",
        "explicit-model-space-wins",
        "explicit-model-joined-wins",
        "explicit-effort-space-wins",
        "explicit-effort-joined-wins",
        "full-ordering",
        "all-none",
        "empty-passthrough-still-adds-model",
        "unknown-effort-dropped",
        "leading-prompt-stays-first",
    ],
)
def test_build_claude_native_base_args(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    expected: tuple[str, ...],
) -> None:
    """
    Assemble base args from persisted launch config.

    Each case pins one invariant; the expected tuple is the exact arg
    vector the runner must hand to ``augment_claude_args``. A mismatch
    means a daemon/server-spawned claude launch would diverge from the
    CLI's command (wrong order, missing pass-through flag, or the model
    override clobbering an explicit user ``--model``).
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
        )
        == expected
    )


@pytest.mark.parametrize(
    (
        "reasoning_effort",
        "model_override",
        "terminal_launch_args",
        "resume",
        "skip_initial_prompt",
        "expected",
    ),
    [
        # Resume alone → just the --resume prefix.
        (None, None, None, "sid-123", False, ("--resume", "sid-123")),
        # --resume comes FIRST, before effort / pass-through / model —
        # mirroring the CLI's (*cold_resume_args, *claude_args) order.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            "sid-123",
            False,
            ("--resume", "sid-123", "--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # No resume id → no --resume (fresh launch, or no local
        # transcript could be synthesized).
        (None, None, ["--verbose"], None, False, ("--verbose",)),
        (
            None,
            None,
            [
                "old prompt\nsecond line",
                "--permission-mode",
                "auto",
                "--mcp-config",
                "a.json",
                "b.json",
            ],
            "sid-123",
            True,
            (
                "--resume",
                "sid-123",
                "--permission-mode",
                "auto",
                "--mcp-config",
                "a.json",
                "b.json",
            ),
        ),
        (None, None, ["old prompt"], "sid-123", True, ("--resume", "sid-123")),
        # An older client's explicit ``--resume -p`` prompt has not run yet.
        (
            None,
            None,
            ["new prompt", "--verbose"],
            "sid-123",
            False,
            ("--resume", "sid-123", "new prompt", "--verbose"),
        ),
        (
            None,
            None,
            ["old prompt", "--model=sonnet", "--verbose"],
            None,
            True,
            ("--model=sonnet", "--verbose"),
        ),
        (
            None,
            None,
            ["--permission-mode", "auto", "--mcp-config", "a.json", "b.json"],
            "sid-123",
            False,
            (
                "--resume",
                "sid-123",
                "--permission-mode",
                "auto",
                "--mcp-config",
                "a.json",
                "b.json",
            ),
        ),
        (
            None,
            None,
            ["--session-id", "11111111-2222-3333-4444-555555555555", "--verbose"],
            "sid-123",
            False,
            ("--resume", "sid-123", "--verbose"),
        ),
        (
            None,
            None,
            ["--session-id=11111111-2222-3333-4444-555555555555", "--verbose"],
            "sid-123",
            False,
            ("--resume", "sid-123", "--verbose"),
        ),
        (
            None,
            None,
            ["--fork-session", "--session-id", "new-id"],
            "sid-123",
            False,
            ("--resume", "sid-123", "--fork-session", "--session-id", "new-id"),
        ),
        (
            None,
            None,
            ["--session-id", "11111111-2222-3333-4444-555555555555"],
            None,
            False,
            ("--session-id", "11111111-2222-3333-4444-555555555555"),
        ),
    ],
    ids=[
        "resume-only",
        "resume-first-ordering",
        "no-resume",
        "legacy-prompt-omitted",
        "legacy-prompt-only",
        "unconsumed-legacy-prompt-kept-on-resume",
        "legacy-prompt-omitted-without-transcript",
        "flag-values-preserved",
        "resume-drops-session-id",
        "resume-drops-joined-session-id",
        "fork-session-keeps-session-id",
        "fresh-launch-keeps-session-id",
    ],
)
def test_build_claude_native_base_args_resume_prefix(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    resume: str | None,
    skip_initial_prompt: bool,
    expected: tuple[str, ...],
) -> None:
    """
    A cold-resume session id is prepended as ``--resume <sid>`` ahead of
    every other arg.

    The ordering matters: Claude applies ``--resume`` to pick the
    transcript, and the runner-side launch must match the CLI's
    long-standing ``--resume``-first arg vector. A wrong position (or a
    missing prefix when an id is supplied) would mean a daemon/web-UI
    resume silently starts a fresh Claude session instead of reopening
    the prior transcript.
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
            resume_external_session_id=resume,
            skip_initial_prompt=skip_initial_prompt,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("terminal_launch_args", "items_status", "pages", "has_prior_history", "expected", "requests"),
    [
        (["--verbose"], 200, [["--verbose"]], True, False, 0),
        (
            ["old prompt\nsecond line", "--verbose"],
            200,
            [["old prompt\nsecond line"]],
            False,
            True,
            1,
        ),
        (["new prompt", "--verbose"], 200, [["old prompt"]], True, False, 1),
        (["resume prompt"], 200, [["old prompt"], ["resume prompt"], ["later"]], True, True, 2),
        (["resume prompt"], 200, [["old prompt"], ["later"]], True, False, 2),
        (["old prompt"], 500, [[]], True, True, 1),
        (["old prompt"], 500, [[]], False, False, 1),
        (["old prompt"], None, [[]], True, True, 0),
    ],
    ids=[
        "no-legacy-prompt",
        "recorded-prompt-consumed",
        "explicit-resume-prompt-kept",
        "prompt-recorded-past-first-page",
        "prompt-on-no-page-kept",
        "unreadable-history-with-prior-session",
        "unreadable-history-fresh-session",
        "offline-with-prior-session",
    ],
)
async def test_claude_legacy_prompt_consumed(
    terminal_launch_args: list[str],
    items_status: int | None,
    pages: list[list[str]],
    has_prior_history: bool,
    expected: bool,
    requests: int,
) -> None:
    """Only a stored prompt already recorded as a user turn is omitted."""
    import httpx

    from omnigent.runner.native.orchestration import _claude_legacy_prompt_consumed

    requested_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_legacy/items"
        assert items_status is not None
        cursor = request.url.params.get("after")
        requested_cursors.append(cursor)
        index = 0 if cursor is None else int(cursor.removeprefix("item_")) + 1
        return httpx.Response(
            items_status,
            json={
                "data": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                    for text in pages[index]
                ],
                "has_more": index < len(pages) - 1,
                "last_id": f"item_{index}",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        consumed = await _claude_legacy_prompt_consumed(
            server_client=client if items_status is not None else None,
            session_id="conv_legacy",
            terminal_launch_args=terminal_launch_args,
            has_prior_history=has_prior_history,
        )

    assert consumed is expected
    assert requested_cursors == [None, "item_0"][:requests]


@pytest.mark.parametrize(("last_id", "requests"), [(None, 1), ("item_0", 2)])
async def test_claude_prompt_in_history_stops_on_a_stalled_cursor(
    last_id: str | None, requests: int
) -> None:
    """A page that claims more items without a new cursor makes history unknown."""
    import httpx

    from omnigent.runner.native.orchestration import _claude_prompt_in_history

    requested_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_cursors.append(request.url.params.get("after"))
        return httpx.Response(200, json={"data": [], "has_more": True, "last_id": last_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        assert await _claude_prompt_in_history(client, "conv_legacy", "old prompt") is None

    assert requested_cursors == [None, "item_0"][:requests]


def test_build_claude_native_base_args_carries_pinned_permission_mode_into_resume() -> None:
    """
    A pinned ``--permission-mode`` reaches the cold-resume argv unchanged.

    The server pins a picker-confirmed mode into ``terminal_launch_args``
    precisely because this builder is the only thing a relaunch consults;
    the pass-through must land after ``--resume`` and survive the
    ``--model`` default so the resumed Claude opens in the chosen mode.
    """
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override="claude-opus-5",
        terminal_launch_args=["--permission-mode", "auto"],
        resume_external_session_id="02857840-6362-408f-b41f-309e396ed7c6",
    )

    assert args == (
        "--resume",
        "02857840-6362-408f-b41f-309e396ed7c6",
        "--permission-mode",
        "auto",
        "--model",
        "claude-opus-5",
    )


def test_claude_terminal_env_unset_masks_credentials_with_api_key_helper() -> None:
    """Ambient keys and tokens must not override the selected helper."""
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    env_unset = _claude_terminal_env_unset(config)
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "ANTHROPIC_AUTH_TOKEN" in env_unset
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_unset
    assert "CLAUDECODE" in env_unset
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset


def test_claude_terminal_env_unset_without_helper_keeps_credentials() -> None:
    """Native login, Bedrock, and token-only gateways keep their credentials."""
    expected = ["DATABRICKS_CONFIG_PROFILE", "CLAUDECODE"]
    own_login_env_unset = _claude_terminal_env_unset(None)
    assert own_login_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in own_login_env_unset
    assert "ANTHROPIC_AUTH_TOKEN" not in own_login_env_unset
    bedrock_like = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example"},
        api_key_helper=None,
        model="us.anthropic.claude-opus-4-5-20251101-v1:0",
    )
    bedrock_env_unset = _claude_terminal_env_unset(bedrock_like)
    assert bedrock_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in bedrock_env_unset
    assert "ANTHROPIC_AUTH_TOKEN" not in bedrock_env_unset
    token_only = ClaudeNativeUcodeConfig(
        env={"CLAUDE_CODE_USE_GATEWAY": "1", "ANTHROPIC_AUTH_TOKEN": "test-token"},
    )
    assert _claude_terminal_env_unset(token_only) == expected
    assert build_native_claude_terminal_env(token_only)["ANTHROPIC_AUTH_TOKEN"] == "test-token"


def test_native_launch_passes_synthesized_model_as_flag() -> None:
    """A synthesized gateway model reaches the launch as ``--model``.

    ``_auto_create_claude_terminal`` feeds ``claude_config.model`` (populated
    by ambient Anthropic synthesis from ``ANTHROPIC_MODEL``) into the base
    args as the model default. This pins the end-to-end contract: a gateway
    model resolves to ``--model <id>`` so Claude Code doesn't launch with its
    own default that the gateway rejects.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    # Mirrors the runner's precedence: session override wins, else the
    # provider/ucode gateway model becomes the --model default.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=None or config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "gateway-served-claude")


def test_routed_launch_model_reaches_the_terminal_env_as_the_custom_slot() -> None:
    """A routed exact id is launchable AND switchable back to mid-session.

    Mirrors the runner's composition: the session override becomes
    ``--model`` and the same value is pinned into Claude Code's custom picker
    slot, which is the only spelling ``/model`` accepts for an id no family
    alias points at (``opus`` here resolves to the newer generation).
    """
    from omnigent.harnesses.claude_native.main import claude_config_with_launch_model_pinned
    from omnigent.models.claude_model_vocabulary import claude_model_command_arg

    config = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
        },
        api_key_helper="printf %s sk-gateway",
        model="databricks-claude-opus-5",
    )
    session_model_override = "databricks-claude-opus-4-8"

    launched = claude_config_with_launch_model_pinned(config, session_model_override)
    assert launched is not None
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=session_model_override,
        terminal_launch_args=None,
    )
    terminal_env = build_native_claude_terminal_env(launched)

    assert args == ("--model", "databricks-claude-opus-4-8")
    assert terminal_env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-opus-4-8"
    assert (
        claude_model_command_arg(session_model_override, terminal_env)
        == "databricks-claude-opus-4-8"
    )


@pytest.mark.parametrize("credential", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_build_native_claude_terminal_env_rejects_credentials_on_helper_path(
    credential: str,
) -> None:
    """Config overrides must not reintroduce credentials that bypass the helper."""
    leaking = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            credential: "sk-leaked",
        },
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    with pytest.raises(RuntimeError, match=credential):
        build_native_claude_terminal_env(leaking)


@pytest.mark.parametrize(
    ("configured", "managed", "ambient", "disabled"),
    [
        ("https://gateway.example/anthropic", None, None, True),
        ("https://api.anthropic.com", None, None, False),
        (None, "https://gateway.example/anthropic", None, True),
        ("https://api.anthropic.com", "https://gateway.example/anthropic", None, True),
        ("https://gateway.example/anthropic", "https://api.anthropic.com", None, False),
        ("", None, "https://gateway.example/anthropic", True),
        (None, None, "https://gateway.example/anthropic", True),
        ("https://api.anthropic.com", None, "https://gateway.example/anthropic", False),
        ("https://gateway.example/anthropic", None, "https://api.anthropic.com", True),
        (None, None, None, False),
    ],
)
def test_claude_terminal_env_disables_refusal_fallback_for_effective_gateway(
    configured: str | None,
    managed: str | None,
    ambient: str | None,
    disabled: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effective Anthropic endpoint decides whether canonical fallback is safe."""
    from omnigent.harnesses.claude_native import main as claude_native

    managed_settings = tmp_path / "managed-settings.json"
    managed_settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": managed} if managed else {}})
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_settings,))
    if ambient is None:
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", ambient)
    config = (
        ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": configured} if configured else {})
        if configured is not None
        else None
    )

    terminal_env = build_native_claude_terminal_env(config)

    assert (terminal_env.get("CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK") == "1") is disabled


@pytest.mark.parametrize(
    ("base_url", "configured_flag", "expected_flag"),
    [
        ("https://gateway.example/anthropic", "0", "1"),
        ("https://api.anthropic.com", "0", "0"),
        ("https://api.anthropic.com", "1", "1"),
    ],
)
def test_claude_terminal_env_refusal_fallback_provider_override(
    base_url: str, configured_flag: str, expected_flag: str
) -> None:
    """Gateway launches force the guard; direct Anthropic keeps explicit preferences."""
    flag = "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"
    config = ClaudeNativeUcodeConfig(env={"ANTHROPIC_BASE_URL": base_url, flag: configured_flag})

    assert build_native_claude_terminal_env(config)[flag] == expected_flag


def test_claude_terminal_env_databricks_gateway_helper_path() -> None:
    """The Databricks ucode/profile gateway session, end to end through the env seams.

    A Databricks-gateway session has an ``apiKeyHelper`` (the Databricks auth
    command), ``ANTHROPIC_BASE_URL`` at the Databricks endpoint, a ucode model,
    and NO raw ``ANTHROPIC_API_KEY``; the runner also carries an ambient
    ``DATABRICKS_CONFIG_PROFILE``. This pins the real-user shape (not just a
    generic gateway): the terminal child must drop the Databricks profile and
    the raw key / nested-session marker, while ``apiKeyHelper`` +
    ``ANTHROPIC_BASE_URL`` + the model override survive so Claude Code still
    authenticates against Databricks via the helper.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://dbc-example.cloud.databricks.com/anthropic"},
        api_key_helper="databricks auth token --host https://dbc-example.cloud.databricks.com",
        model="databricks-claude-opus-4-8",
    )

    # The terminal child strips the ambient Databricks profile plus the raw
    # key / nested-session marker on the helper path.
    env_unset = _claude_terminal_env_unset(config)
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "ANTHROPIC_AUTH_TOKEN" in env_unset
    assert "CLAUDECODE" in env_unset

    # The built terminal env preserves the gateway endpoint and never emits a
    # raw key (routing is via ANTHROPIC_BASE_URL + apiKeyHelper); the Databricks
    # profile is dropped via env_unset, not the built env.
    terminal_env = build_native_claude_terminal_env(config)
    assert terminal_env["ANTHROPIC_BASE_URL"] == (
        "https://dbc-example.cloud.databricks.com/anthropic"
    )
    assert "ANTHROPIC_API_KEY" not in terminal_env
    assert "DATABRICKS_CONFIG_PROFILE" not in terminal_env

    # The gateway model reaches the launch as ``--model`` so Claude Code
    # doesn't start on an Anthropic-direct default the gateway rejects; the
    # apiKeyHelper survives on the config for augment_claude_args to register.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "databricks-claude-opus-4-8")
    assert config.api_key_helper


@pytest.fixture
def bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Yield a bridge dir the claude-native bridge accepts.

    ``augment_claude_args`` validates the bridge dir against the real
    ``$TMPDIR/omnigent-<uid>/claude-native`` root, so a raw ``tmp_path`` is
    rejected. Point the bridge root and its trusted parent at the test's temp
    dir the way ``tests/harnesses/claude_native/test_claude_native_bridge.py`` does.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: Bridge dir under the patched bridge root.
    """
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    return tmp_path


def _augmented(bridge_dir: Path, *, auto_harness: bool) -> list[str]:
    """Run the runner's own claude-native argv composition for one session shape."""
    from omnigent.harnesses.claude_native.bridge import augment_claude_args

    note, allowed = _routed_spawn_launch_args(auto_harness)
    return augment_claude_args(
        ("--model", "databricks-claude-sonnet-5"),
        bridge_dir=bridge_dir,
        python_executable="/venv/bin/python",
        append_system_prompt=note,
        allowed_tools=allowed,
    )


def test_auto_harness_launch_names_the_routed_spawn_tool_and_preapproves_it(
    bridge_dir: Path,
) -> None:
    """An auto-harness Claude launch carries the note AND the tool allowlist.

    Both halves of the live failure: the model reported
    ``mcp__omnigent__sys_session_create`` as nonexistent (no note, and the
    schema is deferred behind tool search), and Claude Code's don't-ask mode
    denied the Omnigent MCP call outright (no ``--allowedTools``).
    """
    args = _augmented(bridge_dir, auto_harness=True)

    note = args[args.index("--append-system-prompt") + 1]
    assert "mcp__omnigent__sys_session_create" in note
    assert "mcp__omnigent__sys_agent_list" in note
    # Bare spellings would send the model looking for a tool Claude does not
    # advertise, which is the bug.
    assert "`sys_session_create`" not in note
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__omnigent__sys_session_create" in allowed
    assert "mcp__omnigent__sys_agent_list" in allowed
    assert "mcp__omnigent__sys_session_send" in allowed
    assert set(_ROUTED_SPAWN_ALLOWED_TOOLS) <= set(allowed)


def test_pinned_harness_launch_argv_is_unchanged(bridge_dir: Path) -> None:
    """A pinned session's argv must stay byte-identical to the pre-change one.

    The routed-spawn note and the tool allowlist are additions for auto-harness
    sessions only; leaking either into a pinned launch would change every
    non-routed native session's command line.
    """
    from omnigent.harnesses.claude_native.bridge import augment_claude_args

    baseline = augment_claude_args(
        ("--model", "databricks-claude-sonnet-5"),
        bridge_dir=bridge_dir,
        python_executable="/venv/bin/python",
    )

    assert _augmented(bridge_dir, auto_harness=False) == baseline
    assert "--append-system-prompt" not in baseline
    assert "--allowedTools" not in baseline


def test_routed_spawn_launch_args_gate_is_off_without_auto_harness() -> None:
    assert _routed_spawn_launch_args(False) == (None, ())
    note, allowed = _routed_spawn_launch_args(True)
    assert note
    assert allowed == _ROUTED_SPAWN_ALLOWED_TOOLS


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--permission-mode", "auto"], "auto"),
        (["--permission-mode=acceptEdits"], "acceptEdits"),
        (["--permission-mode", "secret-value"], None),
        (["--permission-mode"], None),
        (["--model", "opus"], None),
        (["--permission-mode", "auto", "--permission-mode="], "auto"),
        (["--dangerously-skip-permissions"], "bypassPermissions"),
    ],
)
def test_claude_launch_permission_mode_logs_only_known_values(
    args: list[str], expected: str | None
) -> None:
    assert _claude_launch_permission_mode(args) == expected


async def test_claude_launch_metadata_log_includes_permission_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
    )

    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id="conv_auto",
        agent_id="agent",
        snapshot={
            "created_at": 0,
            "updated_at": 0,
            "terminal_launch_args": ["--permission-mode", "auto"],
        },
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _load_claude_launch_metadata(
            server_client=None,  # type: ignore[arg-type]
            session_id="conv_auto",
            session_init=envelope,
        )

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_launch_config_loaded"
    )
    assert "permission_mode=auto" in record.getMessage()
    assert record.attributes == {"permission_mode": "auto"}


async def test_claude_launch_metadata_log_excludes_unknown_permission_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
    )

    unknown_mode = "secret-value"
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id="conv_unknown",
        agent_id="agent",
        snapshot={
            "created_at": 0,
            "updated_at": 0,
            "terminal_launch_args": ["--permission-mode", unknown_mode],
        },
    )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _load_claude_launch_metadata(
            server_client=None,  # type: ignore[arg-type]
            session_id="conv_unknown",
            session_init=envelope,
        )

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_launch_config_loaded"
    )
    assert unknown_mode not in record.getMessage()
    assert unknown_mode not in record.attributes.values()
    assert "permission_mode" not in record.attributes


async def test_legacy_claude_launch_metadata_log_includes_permission_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"terminal_launch_args": ["--permission-mode=plan"]},
        )

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://runner"
        ) as client:
            await _load_legacy_claude_launch_metadata(client, "conv_plan")

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_launch_config_loaded"
    )
    assert "permission_mode=plan" in record.getMessage()
    assert record.attributes == {"permission_mode": "plan"}


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({AUTO_HARNESS_LABEL_KEY: "1"}, None, True),
        # The sentinel is replaced once first-message routing resolves a
        # harness, so a session still carrying it is auto-harness too.
        ({}, "auto", True),
        ({}, "claude-native", False),
        ({AUTO_HARNESS_LABEL_KEY: "0"}, None, False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "label-off", "neither"],
)
def test_envelope_metadata_reads_the_auto_harness_flag(
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
    )

    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id="conv_abc",
        agent_id="agent",
        snapshot={
            "created_at": 0,
            "updated_at": 0,
            "labels": labels,
            "harness_override": harness_override,
        },
    )

    assert _claude_launch_metadata_from_envelope(envelope).auto_harness is expected


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({AUTO_HARNESS_LABEL_KEY: "1"}, None, True),
        ({}, "auto", True),
        ({}, "claude-native", False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "neither"],
)
async def test_legacy_metadata_loader_reads_the_auto_harness_flag(
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    """The removable legacy snapshot path must parse the flag too.

    A server predating the init envelope still answers ``GET /v1/sessions``, and
    an auto-harness session launched through it needs the same note.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"labels": labels, "harness_override": harness_override},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        metadata = await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert metadata.auto_harness is expected


@pytest.mark.parametrize(
    ("respond", "expected_epoch"),
    [
        pytest.param(lambda: httpx.Response(404), None, id="not-found"),
        pytest.param(lambda: httpx.Response(410), None, id="gone"),
        pytest.param(lambda: httpx.Response(200, json={}), 0, id="server-without-epochs"),
        pytest.param(lambda: httpx.Response(200, json={"context_epoch": 2}), 2, id="known"),
    ],
)
async def test_legacy_metadata_only_lets_a_client_error_launch_with_an_unknown_epoch(
    respond: Callable[[], httpx.Response], expected_epoch: int | None
) -> None:
    """A 4xx launches without the hint, once, and must not pass for a session never reset."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return respond()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        metadata = await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert metadata.context_epoch == expected_epoch


@pytest.fixture
def no_launch_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip_delay(delay: float) -> None:
        del delay

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_config_retry_sleep", skip_delay
    )


async def test_legacy_metadata_rides_out_a_transient_session_read_failure(
    no_launch_retry_delay: None,
) -> None:
    responses = [httpx.Response(503), httpx.Response(200, json={"context_epoch": 2})]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        metadata = await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert metadata.context_epoch == 2


@pytest.mark.parametrize(
    "status",
    [pytest.param(503, id="unavailable"), pytest.param(500, id="internal-error")],
)
async def test_legacy_metadata_refuses_when_the_session_read_keeps_failing(
    no_launch_retry_delay: None, status: int
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        with pytest.raises(_ClaudeLaunchRefusedError):
            await _load_legacy_claude_launch_metadata(client, "conv_abc")

    # Reads run under the ensure lock, but a slow server still gets a full 20 s read.
    assert len(requests) == 2
    assert {request.extensions["timeout"]["read"] for request in requests} == {20.0}


async def test_legacy_metadata_refuses_after_repeated_transport_errors(
    no_launch_retry_delay: None,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("server down", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        with pytest.raises(_ClaudeLaunchRefusedError):
            await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "headers", "message", "redrivable", "retry_after_s"),
    [
        pytest.param(401, {}, _CLAUDE_LAUNCH_ACCESS_REFUSED_MESSAGE, False, 0.0, id="signed-out"),
        pytest.param(403, {}, _CLAUDE_LAUNCH_ACCESS_REFUSED_MESSAGE, False, 0.0, id="barred"),
        pytest.param(
            429, {"Retry-After": "7"}, _CLAUDE_LAUNCH_REFUSED_MESSAGE, True, 7.0, id="rate-limited"
        ),
    ],
)
async def test_legacy_metadata_refuses_access_and_rate_limit_answers_at_once(
    status: int, headers: dict[str, str], message: str, redrivable: bool, retry_after_s: float
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers=headers)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        with pytest.raises(_ClaudeLaunchRefusedError) as refused:
            await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert len(requests) == 1
    assert str(refused.value) == message
    assert refused.value.redrivable is redrivable
    assert refused.value.retry_after_s == retry_after_s


@pytest.mark.parametrize(
    "body",
    [pytest.param(b"not json", id="not-json"), pytest.param(b"[1]", id="not-an-object")],
)
async def test_legacy_metadata_refuses_a_malformed_answer_and_logs_why(
    no_launch_retry_delay: None, body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
        base_url="http://runner",
    ) as client:
        with (
            caplog.at_level("WARNING", logger=orchestration._logger.name),
            pytest.raises(_ClaudeLaunchRefusedError) as refused,
        ):
            await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert refused.value.redrivable is True
    refusals = [r.getMessage() for r in caplog.records if "Refusing Claude launch" in r.message]
    assert len(refusals) == 1
    assert "session=conv_abc" in refusals[0]
    assert "launch config" in refusals[0]


async def test_legacy_metadata_refuses_when_the_reads_outlast_their_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(orchestration, "_LAUNCH_SESSION_READ_DEADLINE_S", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(3600)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        with (
            caplog.at_level("WARNING", logger=orchestration._logger.name),
            pytest.raises(_ClaudeLaunchRefusedError),
        ):
            await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert any("reason=TimeoutError" in record.getMessage() for record in caplog.records)


def test_launch_read_deadline_covers_two_reads_and_the_longest_retry_wait() -> None:
    assert orchestration._LAUNCH_SESSION_READ_DEADLINE_S == (
        orchestration._LAUNCH_SESSION_READ_ATTEMPTS * orchestration._LAUNCH_CONFIG_FETCH_TIMEOUT_S
        + orchestration._LAUNCH_CONFIG_RETRY_MAX_DELAY_S
    )


def test_refused_launch_message_reaches_the_owner_verbatim() -> None:
    refusal = _ClaudeLaunchRefusedError(_CLAUDE_LAUNCH_REFUSED_MESSAGE)

    payload = _native_terminal_start_error_payload(refusal, "Claude", session_id="conv_abc")

    assert payload["message"] == (
        "Claude did not start because the Omnigent server did not give a usable answer, so this "
        "session's state could not be confirmed. Your session and chat are safe and nothing was "
        "changed. Omnigent will keep retrying for up to 15 minutes and start Claude when the "
        "server answers. A message that triggered this stays in the chat but was not sent to "
        "Claude, so send it again after Claude starts. If Claude has not started after 15 "
        f"minutes, send a message to try again. Error ID: {payload['error_id']}."
    )


def test_access_refusal_makes_no_retry_promise() -> None:
    refusal = _ClaudeLaunchRefusedError(_CLAUDE_LAUNCH_ACCESS_REFUSED_MESSAGE, redrivable=False)

    payload = _native_terminal_start_error_payload(refusal, "Claude", session_id="conv_abc")

    assert "refused access to this session" in payload["message"]
    assert "safe" in payload["message"]
    assert "Sign in" in payload["message"]
    assert "15 minutes" not in payload["message"]
    assert "retrying" not in payload["message"]


class _RedriveHarness:
    """A refused launch's context with a recorded ensure and a scripted pane."""

    def __init__(
        self,
        respond: Callable[[], httpx.Response],
        *,
        launch_starts_pane: bool,
        refused_launches: int = 0,
        status: str | None = None,
    ) -> None:
        self.ensures: list[tuple[str, str | None]] = []
        self.events: list[dict[str, Any]] = []
        self.pane: object | None = None
        self.launch_starts_pane = launch_starts_pane
        self.refused_launches = refused_launches
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: respond()), base_url="http://runner"
        )
        self.ctx = NativeLaunchContext(
            session_id="conv_abc",
            resource_registry=cast(SessionResourceRegistry, self),
            publish_event=lambda _session_id, event: self.events.append(dict(event)),
            server_client=self.client,
            redrive_launch=self._ensure,
            session_status=lambda _session_id: status,
        )

    async def get_terminal_resource(self, session_id: str, terminal_id: str) -> object | None:
        del session_id, terminal_id
        return self.pane

    async def _ensure(self, session_id: str, harness_name: str | None) -> None:
        self.ensures.append((session_id, harness_name))
        if self.refused_launches > 0:
            self.refused_launches -= 1
            attempt = _claude_redrive_attempt.get()
            assert attempt is not None
            attempt.refusal = _ClaudeLaunchRefusedError(_CLAUDE_LAUNCH_REFUSED_MESSAGE)
        elif self.launch_starts_pane:
            self.pane = object()


@pytest.fixture
def quick_redrive(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestration = "omnigent.runner.native.orchestration"
    monkeypatch.setattr(f"{orchestration}._LAUNCH_REDRIVE_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(f"{orchestration}._LAUNCH_REDRIVE_MAX_DELAY_S", 0.0)
    monkeypatch.setattr(f"{orchestration}._LAUNCH_REDRIVE_WINDOW_S", 0.05)


async def test_redrive_launches_once_when_the_server_answers(quick_redrive: None) -> None:
    harness = _RedriveHarness(lambda: httpx.Response(200, json={}), launch_starts_pane=True)

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == [("conv_abc", "claude-native")]
    assert harness.events == []


async def test_redrive_logs_each_launch_it_starts_with_its_attempt_number(
    quick_redrive: None, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={}), launch_starts_pane=True, refused_launches=1
    )

    with caplog.at_level("INFO", logger=orchestration._logger.name):
        await _redrive_refused_claude_launch(harness.ctx)

    assert [r.getMessage() for r in caplog.records if "Re-driving" in r.message] == [
        "Re-driving refused Claude launch: session=conv_abc attempt=1",
        "Re-driving refused Claude launch: session=conv_abc attempt=2",
    ]


async def _never_answers(*_args: object) -> None:
    await asyncio.Event().wait()


async def test_spec_read_that_never_answers_refuses_and_schedules_the_redrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "_LAUNCH_SPEC_READ_DEADLINE_S", 0.01)
    scheduled: list[str] = []
    monkeypatch.setattr(
        orchestration,
        "_schedule_claude_launch_redrive",
        lambda ctx, first_wait_s=None: scheduled.append(ctx.session_id),
    )
    harness = _RedriveHarness(lambda: httpx.Response(200, json={}), launch_starts_pane=False)

    with pytest.raises(_ClaudeLaunchRefusedError):
        await orchestration._read_claude_launch_spec(harness.ctx, _never_answers)

    assert scheduled == ["conv_abc"]


async def test_launch_config_read_that_never_answers_refuses_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orchestration, "_LAUNCH_SPEC_READ_DEADLINE_S", 0.01)
    session_id = "conv_config_hang"
    registry = MagicMock()
    registry.launch_required_terminal = AsyncMock()

    with pytest.raises(_ClaudeLaunchRefusedError):
        await orchestration._auto_create_claude_terminal(
            session_id,
            registry,
            Mock(),
            server_client=AsyncMock(spec=httpx.AsyncClient),
            session_init=_claude_auto_create_session_init(session_id, tmp_path),
            auth_token_factory=lambda: None,
            resolve_launch_config=_never_answers,
        )

    registry.launch_required_terminal.assert_not_called()


async def test_redrive_stops_at_its_launch_bound_when_launches_keep_failing(
    quick_redrive: None,
) -> None:
    harness = _RedriveHarness(lambda: httpx.Response(200, json={}), launch_starts_pane=False)

    await _redrive_refused_claude_launch(harness.ctx)

    assert len(harness.ensures) == 3
    assert harness.events == []


async def test_refused_redrive_attempts_do_not_spend_the_launch_budget(
    quick_redrive: None,
) -> None:
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={}), launch_starts_pane=True, refused_launches=5
    )

    await _redrive_refused_claude_launch(harness.ctx)

    assert len(harness.ensures) == 6
    assert harness.pane is not None


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(503), id="server-still-down"),
        pytest.param(httpx.Response(429), id="rate-limited"),
        pytest.param(httpx.Response(200, content=b"not json"), id="malformed"),
        pytest.param(httpx.Response(200, json=[1]), id="not-an-object"),
    ],
)
async def test_redrive_keeps_waiting_without_a_usable_answer(
    quick_redrive: None, response: httpx.Response
) -> None:
    harness = _RedriveHarness(lambda: response, launch_starts_pane=True, status="failed")

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == []
    assert harness.events == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param(
            httpx.Response(404), _CLAUDE_LAUNCH_STOPPED_UNAVAILABLE_MESSAGE, id="deleted-meanwhile"
        ),
        pytest.param(httpx.Response(403), _CLAUDE_LAUNCH_ACCESS_REFUSED_MESSAGE, id="barred"),
        pytest.param(
            httpx.Response(200, json={"archived": True}),
            _CLAUDE_LAUNCH_STOPPED_CLOSED_MESSAGE,
            id="archived",
        ),
        pytest.param(
            httpx.Response(200, json={"labels": {"omnigent.closed": "true"}}),
            _CLAUDE_LAUNCH_STOPPED_CLOSED_MESSAGE,
            id="closed-label",
        ),
        pytest.param(
            httpx.Response(200, json={"title": "researcher:auth:closed:conv_abc123"}),
            _CLAUDE_LAUNCH_STOPPED_CLOSED_MESSAGE,
            id="closed-title",
        ),
        pytest.param(
            httpx.Response(200, json={"harness": "codex-native"}),
            _CLAUDE_LAUNCH_STOPPED_HARNESS_MESSAGE,
            id="harness-changed",
        ),
    ],
)
async def test_redrive_stops_and_replaces_the_keep_retrying_status(
    quick_redrive: None,
    response: httpx.Response,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _RedriveHarness(lambda: response, launch_starts_pane=True, status="failed")

    with caplog.at_level("WARNING", logger=orchestration._logger.name):
        await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == []
    assert [event["status"] for event in harness.events] == ["failed"]
    assert harness.events[0]["error"]["message"].startswith(message)
    assert any("Stopped re-driving" in record.getMessage() for record in caplog.records)


async def test_redrive_stop_leaves_a_status_that_has_moved_on(quick_redrive: None) -> None:
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={"archived": True}),
        launch_starts_pane=True,
        status="running",
    )

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == []
    assert harness.events == []


async def test_redrive_leaves_a_session_moved_to_another_runner_to_that_runner(
    quick_redrive: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ID", "runner_here")
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={"runner_id": "runner_there"}),
        launch_starts_pane=True,
        status="failed",
    )

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == []
    assert harness.events == []


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param({"runner_id": "runner_here", "harness": "claude-native"}, id="same"),
        pytest.param({"runner_id": None, "harness": None}, id="unbound"),
        pytest.param({"harness": "auto"}, id="auto-routed"),
    ],
)
async def test_redrive_launches_a_session_still_claude_on_this_runner(
    quick_redrive: None, monkeypatch: pytest.MonkeyPatch, snapshot: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ID", "runner_here")
    harness = _RedriveHarness(lambda: httpx.Response(200, json=snapshot), launch_starts_pane=True)

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == [("conv_abc", "claude-native")]


async def test_redrive_honours_retry_after_from_the_refusal_and_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def record_wait(generation: int, *, timeout_seconds: float) -> int:
        waits.append(timeout_seconds)
        return generation

    monkeypatch.setattr(pending_approvals, "wait_for_server_reconnect", record_wait)
    responses = [
        httpx.Response(429, headers={"Retry-After": "120"}),
        httpx.Response(200, json={}),
    ]
    harness = _RedriveHarness(lambda: responses.pop(0), launch_starts_pane=True)

    await _redrive_refused_claude_launch(harness.ctx, first_wait_s=9.0)

    assert waits == [9.0, 120.0]


class _LoaderLaunch:
    """Runs the real ``_launch_claude`` on the real loader, as a re-drive's ensure does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, harness: _RedriveHarness) -> None:
        self.reads = 0
        self.harness = harness
        monkeypatch.setattr(orchestration, "_auto_create_claude_terminal", self._auto_create)
        harness.ctx = dataclasses.replace(harness.ctx, redrive_launch=self.ensure)

    async def _auto_create(self, session_id: str, *_args: Any, **kwargs: Any) -> object:
        await _load_legacy_claude_launch_metadata(kwargs["server_client"], session_id)
        self.harness.pane = object()
        return self.harness.pane

    async def ensure(self, session_id: str, harness_name: str | None) -> None:
        self.harness.ensures.append((session_id, harness_name))
        # The ensure route turns a refusal into a response.
        with contextlib.suppress(_ClaudeLaunchRefusedError):
            await _launch_claude(self.harness.ctx)


async def test_redrive_never_launches_on_a_client_error_after_the_probe_passed(
    quick_redrive: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [httpx.Response(200, json={}), httpx.Response(404)]
    harness = _RedriveHarness(lambda: responses.pop(0), launch_starts_pane=False)
    _LoaderLaunch(monkeypatch, harness)

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.pane is None
    assert responses == []
    assert len(harness.ensures) == 1


async def test_outside_a_redrive_a_client_error_still_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RedriveHarness(lambda: httpx.Response(404), launch_starts_pane=False)
    _LoaderLaunch(monkeypatch, harness)

    await _launch_claude(harness.ctx)

    assert harness.pane is not None


async def test_access_refusal_schedules_no_redrive(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _RedriveHarness(lambda: httpx.Response(401), launch_starts_pane=False)
    _LoaderLaunch(monkeypatch, harness)

    with pytest.raises(_ClaudeLaunchRefusedError):
        await _launch_claude(harness.ctx)

    assert "conv_abc" not in _claude_launch_redrives


@pytest.fixture
def refused_session() -> Iterator[str]:
    _claude_refused_status_sessions.add("conv_abc")
    yield "conv_abc"
    _claude_refused_status_sessions.discard("conv_abc")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param("failed", ["running", "idle"], id="refusal-still-showing"),
        pytest.param("running", [], id="live-turn"),
        pytest.param("idle", [], id="already-clear"),
    ],
)
async def test_any_successful_launch_clears_a_refusal_without_touching_a_live_turn(
    monkeypatch: pytest.MonkeyPatch, refused_session: str, status: str, expected: list[str]
) -> None:
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={}), launch_starts_pane=False, status=status
    )
    _LoaderLaunch(monkeypatch, harness)

    await _launch_claude(harness.ctx)

    assert [event["status"] for event in harness.events] == expected
    assert refused_session not in _claude_refused_status_sessions


async def test_a_launch_leaves_a_failure_it_did_not_refuse_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RedriveHarness(
        lambda: httpx.Response(200, json={}), launch_starts_pane=False, status="failed"
    )
    _LoaderLaunch(monkeypatch, harness)

    await _launch_claude(harness.ctx)

    assert harness.events == []


def test_only_a_refused_launch_marks_the_status_for_clearing() -> None:
    def publish(_session_id: str, _event: dict[str, Any]) -> None:
        return None

    refusal = _ClaudeLaunchRefusedError(_CLAUDE_LAUNCH_REFUSED_MESSAGE)
    _publish_native_terminal_start_error(publish, "conv_marked", "Claude", refusal)
    assert "conv_marked" in _claude_refused_status_sessions

    _publish_native_terminal_start_error(publish, "conv_marked", "Claude", RuntimeError("boom"))
    assert "conv_marked" not in _claude_refused_status_sessions


async def test_deleting_a_session_cancels_its_redrive(refused_session: str) -> None:
    redrive = asyncio.create_task(asyncio.sleep(3600))
    _claude_launch_redrives[refused_session] = redrive

    try:
        _cancel_claude_launch_redrive(refused_session)
        await asyncio.gather(redrive, return_exceptions=True)
    finally:
        _claude_launch_redrives.pop(refused_session, None)

    assert redrive.cancelled()
    assert refused_session not in _claude_refused_status_sessions


async def test_redrive_leaves_a_pane_someone_else_started_alone(quick_redrive: None) -> None:
    harness = _RedriveHarness(lambda: httpx.Response(200, json={}), launch_starts_pane=True)
    harness.pane = object()

    await _redrive_refused_claude_launch(harness.ctx)

    assert harness.ensures == []
    assert harness.events == []


async def test_redrive_wakes_on_tunnel_reconnect_rather_than_its_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._LAUNCH_REDRIVE_BASE_DELAY_S", 3600.0
    )
    harness = _RedriveHarness(lambda: httpx.Response(200, json={}), launch_starts_pane=True)
    redrive = asyncio.create_task(_redrive_refused_claude_launch(harness.ctx))
    await asyncio.sleep(0.01)
    assert harness.ensures == []

    pending_approvals.notify_server_reconnect()
    await asyncio.wait_for(redrive, timeout=5)

    assert harness.ensures == [("conv_abc", "claude-native")]


async def test_runner_launch_error_is_logged_before_cancellable_diagnostic_drain(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelling a blocked diagnostic drain must leave the launch error logged."""
    import asyncio
    import contextlib
    import logging
    import threading
    from unittest.mock import AsyncMock, Mock

    import httpx

    from omnigent.harnesses.claude_native import diagnostics
    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.ensure_claude_workspace_trusted",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("omnigent.inference_config.load_runtime_inference_config", dict)
    monkeypatch.setattr("omnigent.config.load_effective_config", dict)
    monkeypatch.setattr(orchestration, "resolve_cli_binary", lambda _: None)
    # Keep application traceback renderers out of this synchronization test.
    logger = logging.getLogger(f"{__name__}.launch_failure")
    caplog.set_level(logging.ERROR, logger=logger.name)
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(orchestration, "_logger", logger)
    session_id = "conv_launch_cancelled_during_drain"
    original_error = httpx.ConnectError("original launch transport failure")
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}
    registry.launch_required_terminal.side_effect = original_error
    session_init = _claude_auto_create_session_init(session_id, bridge_dir)
    loop = asyncio.get_running_loop()
    closing = asyncio.Event()
    closed = asyncio.Event()
    release_close = threading.Event()

    def blocked_close(_session_id: str) -> None:
        loop.call_soon_threadsafe(closing.set)
        try:
            if not release_close.wait(timeout=30):
                raise TimeoutError("test did not release diagnostic close")
        finally:
            loop.call_soon_threadsafe(closed.set)

    follower = Mock(close=Mock(side_effect=blocked_close))
    monkeypatch.setattr(diagnostics, "ClaudeDebugLogFollower", lambda _: follower)
    task = asyncio.create_task(
        orchestration._auto_create_claude_terminal(
            session_id,
            registry,
            Mock(),
            server_client=AsyncMock(spec=httpx.AsyncClient),
            session_init=session_init,
            auth_token_factory=lambda: None,
            resolve_launch_config=AsyncMock(return_value=None),
        )
    )
    try:
        await asyncio.wait_for(closing.wait(), timeout=10)
        assert not closed.is_set()
        errors_before_cancel = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Claude terminal tmux launch failed:")
        ]
        assert len(errors_before_cancel) == 1
        record = errors_before_cancel[0]
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None
        assert record.exc_info[1] is original_error
        assert getattr(record, "session_id", None) == session_id
        assert f"session={session_id}" in record.getMessage()
        task.cancel("cancelled during diagnostic cleanup")
        with pytest.raises(asyncio.CancelledError, match="cancelled during diagnostic cleanup"):
            await asyncio.wait_for(task, timeout=10)
        assert task.cancelled()
        assert not release_close.is_set()
        assert not closed.is_set()
        registry.launch_required_terminal.assert_awaited_once()
        follower.close.assert_called_once_with(session_id)
    finally:
        release_close.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        if closing.is_set():
            await asyncio.wait_for(closed.wait(), timeout=10)


@pytest.mark.parametrize(
    "command,args,spec_env,env_unset,inherit_env,expected_home,expected_config_dir",
    [
        # The pane inherits the runner env, so its selectors apply.
        ("claude", [], {}, [], True, "/home/runner", "/runner/claude"),
        # Without inheritance only the spec's explicit overrides remain.
        ("claude", [], {"HOME": "/home/spec"}, [], False, "/home/spec", None),
        # Spec overrides and removals are applied before launch.
        (
            "claude",
            [],
            {"CLAUDE_CONFIG_DIR": "/spec/claude"},
            [],
            True,
            "/home/runner",
            "/spec/claude",
        ),
        ("claude", [], {}, ["CLAUDE_CONFIG_DIR"], True, "/home/runner", None),
        # An ``env`` wrapper's assignments and -i apply after the spec.
        (
            "env",
            ["CLAUDE_CONFIG_DIR=/wrap/claude", "claude"],
            {},
            [],
            True,
            "/home/runner",
            "/wrap/claude",
        ),
        ("env", ["-i", "HOME=/home/wrapped", "claude"], {}, [], True, "/home/wrapped", None),
        # ``-S`` words are scanned for options again. ``tests/host`` checks the
        # remaining wrapper syntax against the real ``env``.
        (
            "env",
            ["-S", "- HOME=/split-home CLAUDE_CONFIG_DIR=/launch/claude claude"],
            {},
            [],
            True,
            "/split-home",
            "/launch/claude",
        ),
        # ``-S`` expands ``${NAME}`` from the pre-wrapper environment.
        (
            "env",
            ["-S", "CLAUDE_CONFIG_DIR=${HOME}/claude claude"],
            {},
            [],
            True,
            "/home/runner",
            "/home/runner/claude",
        ),
    ],
)
def test_claude_terminal_launch_env_matches_pane_environment(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    args: list[str],
    spec_env: dict[str, str],
    env_unset: list[str],
    inherit_env: bool,
    expected_home: str,
    expected_config_dir: str | None,
) -> None:
    """
    Trust seeding sees the environment the Claude pane process starts with.

    :param command: Launch command, e.g. ``"env"``.
    :param args: Launch args, e.g. ``["CLAUDE_CONFIG_DIR=/wrap/claude", "claude"]``.
    :param spec_env: Terminal spec env overrides.
    :param env_unset: Terminal spec env removals.
    :param inherit_env: Whether the pane inherits the runner environment.
    :param expected_home: Expected ``HOME`` in the launch env.
    :param expected_config_dir: Expected ``CLAUDE_CONFIG_DIR``, or ``None`` when unset.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration

    monkeypatch.setenv("HOME", "/home/runner")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/runner/claude")
    spec = TerminalEnvSpec(
        command=command, args=args, env=spec_env, env_unset=env_unset, inherit_env=inherit_env
    )

    env = orchestration._claude_terminal_launch_env(spec)

    assert env.get("HOME") == expected_home
    assert env.get("CLAUDE_CONFIG_DIR") == expected_config_dir
    if not inherit_env:
        # Ambient runner selectors must not leak into a non-inheriting pane,
        # which adds only its UTF-8 locale default.
        assert {key: value for key, value in env.items() if key not in ("LANG", "LC_ALL")} == (
            spec_env
        )


def test_claude_terminal_launch_env_expands_the_pane_locale_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env -S`` sees the UTF-8 locale the pane adds when the runner sets none."""
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration

    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    spec = TerminalEnvSpec(command="env", args=["-S", "CLAUDE_CONFIG_DIR=/cfg/${LANG} claude"])

    assert orchestration._claude_terminal_launch_env(spec)["CLAUDE_CONFIG_DIR"] == "/cfg/C.UTF-8"


@pytest.mark.parametrize("secret_name", sorted(RUNNER_AUTH_SECRET_ENV_VARS))
def test_claude_trust_seeding_never_expands_runner_auth_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret_name: str,
) -> None:
    """
    Runner-auth secrets are removed before ``env -S`` expansion, as in the pane.

    Neither the runner environment nor a spec override can put the value into
    the selected config directory or the workspace.

    :param secret_name: A runner-auth secret variable name.
    """
    from omnigent.harnesses.claude_native.bridge import ensure_claude_workspace_trusted
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration

    token = "dummy-binding-token"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    monkeypatch.setenv(secret_name, token)
    spec = TerminalEnvSpec(
        command="env",
        args=["-S", f"CLAUDE_CONFIG_DIR=cfg${{{secret_name}}} claude"],
        env={secret_name: token},
    )

    env = orchestration._claude_terminal_launch_env(spec)
    ensure_claude_workspace_trusted(workspace, env=env)

    assert secret_name not in env
    assert env["CLAUDE_CONFIG_DIR"] == "cfg"
    assert (workspace / "cfg" / ".claude.json").is_file()
    assert not any(token in str(path) for path in tmp_path.rglob("*"))


def test_claude_terminal_launch_cwd_uses_terminal_os_env_resolution(tmp_path: Path) -> None:
    """
    Trust seeding resolves the pane cwd the way terminal creation does.

    An explicit ``os_env`` cwd wins, and ``inherit`` takes the parent os_env's
    cwd, both resolved through symlinks like the launched pane's cwd.
    """
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.runner.native import orchestration

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "link"
    link.symlink_to(workspace)
    explicit = TerminalEnvSpec(os_env=OSEnvSpec(type="caller_process", cwd=str(link)))
    inherited = TerminalEnvSpec(os_env="inherit")
    parent = OSEnvSpec(type="caller_process", cwd=str(link))

    assert orchestration._claude_terminal_launch_cwd(explicit, None) == workspace.resolve()
    assert orchestration._claude_terminal_launch_cwd(inherited, parent) == workspace.resolve()


def test_claude_terminal_launch_cwd_applies_env_chdir(tmp_path: Path) -> None:
    """An ``env --chdir`` wrapper moves the seeded cwd to Claude's real cwd."""
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.runner.native import orchestration

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    os_env = OSEnvSpec(type="caller_process", cwd=str(workspace))
    for args in (["--chdir=sub", "claude"], ["-C", str(workspace / "sub"), "claude"]):
        spec = TerminalEnvSpec(command="env", args=args, os_env=os_env)
        assert (
            orchestration._claude_terminal_launch_cwd(spec, None) == (workspace / "sub").resolve()
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any directory")
def test_claude_terminal_launch_cwd_rejects_an_unsearchable_env_chdir_target(
    tmp_path: Path,
) -> None:
    """``env -C`` cannot enter a directory without search permission, so seeding stops."""
    from omnigent.errors import ErrorCode
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.runner.native import orchestration

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o600)
    spec = TerminalEnvSpec(
        command="env",
        args=["-C", "locked", "claude"],
        os_env=OSEnvSpec(type="caller_process", cwd=str(tmp_path)),
    )
    try:
        with pytest.raises(OmnigentError) as excinfo:
            orchestration._claude_terminal_launch_cwd(spec, None)
    finally:
        locked.chmod(0o700)
    assert excinfo.value.code == ErrorCode.WORKSPACE_MISSING


@pytest.mark.parametrize(
    "args",
    [
        ["-v", "CLAUDE_CONFIG_DIR=wrap/claude", "claude"],
        ["-S", "CLAUDE_CONFIG_DIR='wrap/claude claude"],
        ["-S", "CLAUDE_CONFIG_DIR=$HOME/claude claude"],
        ["-S", "CLAUDE_CONFIG_DIR=wrap\\_claude claude"],
        ["-S", "CLAUDE_CONFIG_DIR=wrap/claude claude #comment"],
        ["-S", "CLAUDE_CONFIG_DIR=wrap/claude\vclaude"],
        ["A=1", "env", "CLAUDE_CONFIG_DIR=wrap/claude", "claude"],
    ],
    ids=[
        "unmodeled-option",
        "unbalanced-quote",
        "variable-expansion",
        "escape",
        "comment",
        "version-dependent-separator",
        "nested-env",
    ],
)
def test_claude_terminal_launch_env_keeps_pre_wrapper_env_for_unparsed_env_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    args: list[str],
) -> None:
    """
    Unmodeled wrappers use the pre-wrapper environment and log the fallback without values.

    :param args: Wrapper args the parser cannot model.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration

    runner_config = tmp_path / "runner-claude"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(runner_config))
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)

    with caplog.at_level(logging.INFO, logger=orchestration._logger.name):
        env = orchestration._claude_terminal_launch_env(TerminalEnvSpec(command="env", args=args))
    assert env["CLAUDE_CONFIG_DIR"] == str(runner_config)
    assert any(
        record.levelno == logging.INFO and "env wrapper form is not modeled" in record.getMessage()
        for record in caplog.records
    )
    values = [
        arg.partition("CLAUDE_CONFIG_DIR=")[2] for arg in args if "CLAUDE_CONFIG_DIR=" in arg
    ]
    assert values and all(value not in caplog.text for value in values)


def test_unparsed_env_wrapper_seeds_the_runner_config_not_a_literal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Seeding with a fallback launch env writes the runner's selected config.

    The unexpanded ``$HOME`` text must not become a literal directory in the
    workspace.
    """
    from omnigent.harnesses.claude_native.bridge import ensure_claude_workspace_trusted
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration

    runner_config = tmp_path / "runner-claude"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(runner_config))
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    spec = TerminalEnvSpec(command="env", args=["-S", "CLAUDE_CONFIG_DIR=$HOME/claude claude"])

    ensure_claude_workspace_trusted(workspace, env=orchestration._claude_terminal_launch_env(spec))

    assert (runner_config / ".claude.json").is_file()
    assert sorted(path.name for path in workspace.iterdir()) == []


def _claude_auto_create_session_init(session_id: str, workspace: Path):
    """
    Build the session-init envelope ``_auto_create_claude_terminal`` consumes.

    :param session_id: Session id, e.g. ``"conv_seed_launch_env"``.
    :param workspace: Session workspace, e.g. the ``bridge_dir`` fixture.
    :returns: A :class:`RunnerSessionInitEnvelope` rooted at *workspace*.
    """
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )

    return RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id=session_id,
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(created_at=0, updated_at=0, workspace=str(workspace)),
    )


async def _auto_create_claude_terminal_for_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str,
    workspace: Path,
    registry: object,
) -> None:
    """
    Run ``_auto_create_claude_terminal`` with host config and diagnostics stubbed.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param session_id: Session id, e.g. ``"conv_seed_launch_env"``.
    :param workspace: Session workspace, e.g. the ``bridge_dir`` fixture.
    :param registry: Session resource registry double.
    :returns: None. Raises whatever the auto-create path raises.
    """
    from unittest.mock import AsyncMock, Mock

    import httpx

    from omnigent.harnesses.claude_native import diagnostics
    from omnigent.runner.native import orchestration

    monkeypatch.setattr("omnigent.inference_config.load_runtime_inference_config", dict)
    monkeypatch.setattr("omnigent.config.load_effective_config", dict)
    monkeypatch.setattr(orchestration, "resolve_cli_binary", lambda _: None)
    monkeypatch.setattr(diagnostics, "ClaudeDebugLogFollower", lambda _: Mock())
    await orchestration._auto_create_claude_terminal(
        session_id,
        registry,
        Mock(),
        server_client=AsyncMock(spec=httpx.AsyncClient),
        session_init=_claude_auto_create_session_init(session_id, workspace),
        auth_token_factory=lambda: None,
        resolve_launch_config=AsyncMock(return_value=None),
    )


async def test_auto_create_claude_terminal_seeds_trust_with_launch_env(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Terminal auto-create hands the pane's launch env to trust seeding.

    A launch-only ``CLAUDE_CONFIG_DIR`` (here from an ``env`` wrapper) must
    reach the seeder, and seeding must happen before the terminal launches.
    """
    from unittest.mock import Mock

    from omnigent.runner.resource_registry import SessionResourceRegistry

    seeded: list[tuple[Path, dict[str, str] | None]] = []
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}
    registry.launch_required_terminal.side_effect = RuntimeError("stop after seeding")

    def record_seed(workspace: Path, env: dict[str, str] | None = None) -> None:
        registry.launch_required_terminal.assert_not_called()
        seeded.append((workspace, env))

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/runner/claude")
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.ensure_claude_workspace_trusted", record_seed
    )
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_command", lambda *_a, **_k: "env"
    )
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_args",
        lambda _harness, cli_args, cfg=None: [
            "CLAUDE_CONFIG_DIR=/launch/claude",
            "claude",
            *cli_args,
        ],
    )

    with pytest.raises(RuntimeError, match="stop after seeding"):
        await _auto_create_claude_terminal_for_test(
            monkeypatch, session_id="conv_seed_launch_env", workspace=bridge_dir, registry=registry
        )

    assert len(seeded) == 1
    workspace, env = seeded[0]
    assert workspace == Path(bridge_dir).resolve()
    assert env is not None
    assert env["CLAUDE_CONFIG_DIR"] == "/launch/claude"
    registry.launch_required_terminal.assert_awaited_once()


@pytest.mark.parametrize(
    "subagent_started,turn_started",
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_auto_create_claude_terminal_cleans_routers_when_trust_seeding_fails(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    subagent_started: bool,
    turn_started: bool,
) -> None:
    """
    A trust-seeding failure releases only the routers this launch started.

    Seeding runs after the subagent and turn routers start but before the
    forwarder that normally shuts them down exists, so a malformed config must
    not leave them running. A router this launch did not start is not shut
    down, because a ``None`` handle would close whichever router a replacement
    session registered.

    :param subagent_started: Whether this launch started a subagent router.
    :param turn_started: Whether this launch started a turn router.
    """
    from unittest.mock import Mock

    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry

    subagent_router = object() if subagent_started else None
    turn_router = object() if turn_started else None
    shutdowns: list[tuple[str, object]] = []

    async def record_subagent_shutdown(_session_id: str, router: object) -> None:
        shutdowns.append(("subagent", router))

    async def record_turn_shutdown(_session_id: str, router: object) -> None:
        shutdowns.append(("turn", router))

    def failing_seed(_workspace: Path, env: dict[str, str] | None = None) -> None:
        raise ValueError("config is not a JSON object")

    monkeypatch.setattr(
        orchestration,
        "_start_subagent_router_for_native_session",
        lambda *_a, **_k: (bridge_dir if subagent_started else None, subagent_router),
    )
    monkeypatch.setattr(
        orchestration, "_start_turn_router_for_native_session", lambda *_a, **_k: turn_router
    )
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", record_subagent_shutdown)
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", record_turn_shutdown)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.ensure_claude_workspace_trusted", failing_seed
    )
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}

    with pytest.raises(ValueError, match="config is not a JSON object"):
        await _auto_create_claude_terminal_for_test(
            monkeypatch,
            session_id="conv_seed_failure_routers",
            workspace=bridge_dir,
            registry=registry,
        )

    expected = []
    if subagent_started:
        expected.append(("subagent", subagent_router))
    if turn_started:
        expected.append(("turn", turn_router))
    assert shutdowns == expected
    registry.launch_required_terminal.assert_not_called()


@pytest.mark.parametrize(
    "shutdown_error,propagated",
    [
        (RuntimeError("subagent shutdown failed"), ValueError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["failed", "cancelled"],
)
async def test_auto_create_claude_terminal_seed_cleanup_attempts_both_shutdowns(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown_error: BaseException,
    propagated: type[BaseException],
) -> None:
    """
    A failed or cancelled router shutdown does not skip the other one.

    A shutdown failure is logged and the trust-seeding error propagates. A
    cancellation propagates after both shutdowns have run.

    :param shutdown_error: What the first shutdown raises.
    :param propagated: The exception type the launch must raise.
    """
    from unittest.mock import Mock

    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry

    subagent_router = object()
    turn_router = object()
    shutdowns: list[str] = []

    async def failing_subagent_shutdown(_session_id: str, _router: object) -> None:
        shutdowns.append("subagent")
        raise shutdown_error

    async def record_turn_shutdown(_session_id: str, _router: object) -> None:
        shutdowns.append("turn")

    def failing_seed(_workspace: Path, env: dict[str, str] | None = None) -> None:
        raise ValueError("config is not a JSON object")

    monkeypatch.setattr(
        orchestration,
        "_start_subagent_router_for_native_session",
        lambda *_a, **_k: (bridge_dir, subagent_router),
    )
    monkeypatch.setattr(
        orchestration, "_start_turn_router_for_native_session", lambda *_a, **_k: turn_router
    )
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", failing_subagent_shutdown)
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", record_turn_shutdown)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.ensure_claude_workspace_trusted", failing_seed
    )
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}

    with pytest.raises(propagated):
        await _auto_create_claude_terminal_for_test(
            monkeypatch, session_id="conv_cleanup_failure", workspace=bridge_dir, registry=registry
        )

    assert shutdowns == ["subagent", "turn"]
    registry.launch_required_terminal.assert_not_called()


@pytest.mark.parametrize(
    "config_dir,legacy_in_cwd,expected",
    [("relcfg", False, "sub/relcfg/.claude.json"), ("", True, "sub/.config.json")],
)
async def test_auto_create_claude_terminal_seeds_env_chdir_with_relative_config_dir(
    bridge_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_dir: str,
    legacy_in_cwd: bool,
    expected: str,
) -> None:
    """
    ``env -C`` and a relative or empty ``CLAUDE_CONFIG_DIR`` compose in auto-create.

    The wrapper moves Claude's cwd first; the config dir then resolves against
    that cwd, and the trust key names it. The real seeder writes the file.

    :param config_dir: ``CLAUDE_CONFIG_DIR`` assigned by the wrapper.
    :param legacy_in_cwd: Whether a legacy ``.config.json`` exists in the cwd.
    :param expected: Expected config file relative to the workspace.
    """
    import json
    from unittest.mock import Mock

    from omnigent.runner.resource_registry import SessionResourceRegistry

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    if legacy_in_cwd:
        (workspace / "sub" / ".config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_command", lambda *_a, **_k: "env"
    )
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_args",
        lambda _harness, cli_args, cfg=None: [
            "-C",
            "sub",
            f"CLAUDE_CONFIG_DIR={config_dir}",
            "claude",
            *cli_args,
        ],
    )
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}
    registry.launch_required_terminal.side_effect = RuntimeError("stop after seeding")

    with pytest.raises(RuntimeError, match="stop after seeding"):
        await _auto_create_claude_terminal_for_test(
            monkeypatch, session_id="conv_seed_env_chdir", workspace=workspace, registry=registry
        )

    data = json.loads((workspace / expected).read_text())
    assert data["projects"][str((workspace / "sub").resolve())]["hasTrustDialogAccepted"] is True
    assert not (tmp_path / "home" / ".claude.json").exists()


@pytest.mark.parametrize(
    "chdir_args",
    [["-C", "missing"], ["-C", ""], ["--chdir="]],
    ids=["missing", "empty", "empty-long"],
)
async def test_auto_create_claude_terminal_rejects_missing_env_chdir_target(
    bridge_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chdir_args: list[str],
) -> None:
    """
    A missing or empty ``env --chdir`` target fails before seeding and launch.

    The real ``env`` refuses to start in such a directory, so seeding must not
    create it (and with it a trusted config) and turn the launch into a
    success.

    :param chdir_args: The wrapper's ``-C``/``--chdir`` arguments.
    """
    from unittest.mock import Mock

    from omnigent.errors import ErrorCode
    from omnigent.runner.resource_registry import SessionResourceRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Relative, so a wrongly accepted target would put it under that cwd.
    config_dir = f"{tmp_path.name}-config"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_command", lambda *_a, **_k: "env"
    )
    monkeypatch.setattr(
        "omnigent.harness_startup_config.resolve_harness_args",
        lambda _harness, cli_args, cfg=None: [
            *chdir_args,
            f"CLAUDE_CONFIG_DIR={config_dir}",
            "claude",
            *cli_args,
        ],
    )
    registry = Mock(spec=SessionResourceRegistry)
    registry.client_surface_env.return_value = {"OMNIGENT_CLIENT_SURFACE": "desktop"}

    with pytest.raises(OmnigentError) as excinfo:
        await _auto_create_claude_terminal_for_test(
            monkeypatch, session_id="conv_missing_chdir", workspace=workspace, registry=registry
        )

    assert excinfo.value.code == ErrorCode.WORKSPACE_MISSING
    assert sorted(path.name for path in workspace.iterdir()) == []
    assert not (Path.cwd() / config_dir).exists()
    registry.launch_required_terminal.assert_not_called()


def test_claude_terminal_env_unsets_tmux_for_bash_tool_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude's Bash commands must not reach the private tmux server running Claude."""
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    env_file = build_native_claude_terminal_env(None)["CLAUDE_ENV_FILE"]

    # Claude Code sources the env file before each Bash command it runs.
    result = subprocess.run(
        ["bash", "-c", f'. {shlex.quote(env_file)}; echo "${{TMUX-unset}} ${{TMUX_PANE-unset}}"'],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMUX": "/tmp/omnigent-terminal-ab12/tmux.sock,4242,0",
            "TMUX_PANE": "%0",
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "unset unset"


@pytest.mark.parametrize("source", ["ambient", "provider"])
def test_claude_terminal_env_keeps_a_user_claude_env_file(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """A user's own CLAUDE_ENV_FILE is left in place instead of the tmux scrub."""
    user_file = "/home/user/claude-env.sh"
    config = None
    if source == "ambient":
        monkeypatch.setenv("CLAUDE_ENV_FILE", user_file)
    else:
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        config = ClaudeNativeUcodeConfig(env={"CLAUDE_ENV_FILE": user_file})

    terminal_env = build_native_claude_terminal_env(config)

    assert terminal_env.get("CLAUDE_ENV_FILE", user_file) == user_file

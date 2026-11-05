"""Convenience host shells for ACP picker agents.

The seeded bundle stays free of ``terminals:`` so ``sys_terminal_*`` is not
registered. The side shell is a launch-time overlay for that unsandboxed
picker shape only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.runner.identity import RUNNER_INTERACTIVE_SHELLS_ENV_VAR
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.acp_picker_shells import (
    acp_picker_convenience_shells_allowed,
    convenience_shell_launch,
)


def _spec(
    *,
    name: str,
    harness: str,
    sandbox: str = "none",
    terminals: dict[str, TerminalEnvSpec] | None = None,
    os_env: OSEnvSpec | object | None = "default",
) -> AgentSpec:
    """Build the picker-shaped spec the tests vary one field at a time."""
    if os_env == "default":
        os_env = OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type=sandbox),
        )
    return AgentSpec(
        spec_version=1,
        name=name,
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
        os_env=os_env,  # type: ignore[arg-type]
        terminals=terminals,
    )


def test_unsandboxed_acp_picker_offers_convenience_shells() -> None:
    """Builtin and configured picker rows with an empty terminal block qualify."""
    assert acp_picker_convenience_shells_allowed(_spec(name="grok", harness="grok"))
    assert acp_picker_convenience_shells_allowed(_spec(name="grok", harness="grok-build"))
    assert acp_picker_convenience_shells_allowed(_spec(name="devin", harness="acp:devin"))


def test_custom_sandboxed_and_declared_specs_stay_closed() -> None:
    """An empty terminals block is not by itself permission to open a host shell."""
    assert not acp_picker_convenience_shells_allowed(
        _spec(name="custom-agent", harness="claude-sdk")
    )
    assert not acp_picker_convenience_shells_allowed(_spec(name="helper", harness="grok"))
    assert not acp_picker_convenience_shells_allowed(_spec(name="devin", harness="acp:other"))
    assert not acp_picker_convenience_shells_allowed(
        _spec(name="grok", harness="grok", sandbox="linux_bwrap")
    )
    assert not acp_picker_convenience_shells_allowed(
        _spec(name="grok", harness="grok", os_env=None)
    )
    assert not acp_picker_convenience_shells_allowed(
        _spec(
            name="jcode",
            harness="jcode",
            terminals={"bash": TerminalEnvSpec(command="bash")},
        )
    )


def test_convenience_launch_uses_host_inventory_without_mutating_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch spec is unsandboxed caller_process and stays off the agent spec."""
    monkeypatch.setenv(RUNNER_INTERACTIVE_SHELLS_ENV_VAR, '["zsh", "bash", "nope"]')
    spec = _spec(name="grok", harness="grok")

    launch = convenience_shell_launch(spec, "zsh")

    assert launch is not None
    assert launch.available == ("zsh", "bash")
    assert launch.spec is not None
    assert launch.spec.command is not None
    assert launch.spec.command.endswith("zsh")
    assert launch.spec.allow_cwd_override is True
    assert isinstance(launch.spec.os_env, OSEnvSpec)
    assert launch.spec.os_env.type == "caller_process"
    assert launch.spec.os_env.sandbox is not None
    assert launch.spec.os_env.sandbox.type == "none"
    assert spec.terminals is None


def test_convenience_launch_rejects_a_shell_the_host_did_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published inventory that lacks the name does not fall open to the machine."""
    monkeypatch.setenv(RUNNER_INTERACTIVE_SHELLS_ENV_VAR, '["bash"]')
    spec = _spec(name="grok", harness="grok")

    launch = convenience_shell_launch(spec, "zsh")

    assert launch is not None
    assert launch.spec is None
    assert launch.available == ("bash",)


def test_convenience_launch_uses_installed_shells_when_no_inventory_was_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local runner with no host snapshot still launches a shell that is installed."""
    monkeypatch.delenv(RUNNER_INTERACTIVE_SHELLS_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "omnigent._platform.installed_interactive_shells",
        lambda: ["fish", "bash"],
    )
    spec = _spec(name="jcode", harness="jcode")

    opened = convenience_shell_launch(spec, "fish")
    missing = convenience_shell_launch(spec, "zsh")

    assert opened is not None and opened.spec is not None
    assert opened.spec.command is not None
    assert opened.spec.command.endswith("fish")
    assert missing is not None and missing.spec is None
    assert missing.available == ("fish", "bash")
    assert spec.terminals is None


def test_convenience_launch_ignores_a_spec_that_is_not_a_picker() -> None:
    """Custom agents keep the synthesize path, including a same-shaped os_env."""
    spec = SimpleNamespace(
        name="custom-agent",
        executor=SimpleNamespace(config={"harness": "claude-sdk"}, type="omnigent"),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        terminals=None,
    )
    assert convenience_shell_launch(spec, "zsh") is None

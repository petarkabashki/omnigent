"""Convenience host shells for ACP picker sessions.

Seeded ACP picker bundles omit ``terminals:``. A non-empty block registers
``sys_terminal_*`` for the model, and these agents already have a shell
through their own CLI. The side pane is decided when the session agent is
read and when a pane is launched: the host's interactive shells, spawned
with the same unsandboxed ``caller_process`` spec native wrappers use.

A custom agent that omits ``terminals:`` stays closed. So does a sandboxed
spec. That empty block is what keeps it from opening a host shell.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from omnigent.inner.datamodel import TerminalEnvSpec


def acp_picker_identity(name: str | None, harness: str | None) -> bool:
    """Return whether *name* and *harness* are a seeded ACP picker row.

    Builtin rows are keyed by their catalog id (``grok``) and carry that
    harness. Configured rows are keyed by slug and carry ``acp:<slug>``.
    A custom spec that merely uses some other harness is not a picker row.

    :param name: Agent spec name, e.g. ``"grok"`` or ``"devin"``.
    :param harness: Executor harness id, e.g. ``"grok"`` or ``"acp:devin"``.
    :returns: ``True`` for a seeded picker identity.
    """
    if not name or not harness:
        return False
    # Check the raw ``acp:<slug>`` form first. Canonicalizing that string
    # collapses every configured agent to the base ``acp`` harness.
    if harness == f"acp:{name}":
        return True
    from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
    from omnigent.harness_aliases import canonicalize_harness

    canonical = canonicalize_harness(harness) or harness
    return name in ACP_CLI_HARNESSES and canonical == name


def acp_picker_convenience_shells_allowed(spec: object) -> bool:
    """Return whether *spec* may offer host shells without declaring them.

    Allowed only for an ACP picker whose own ``terminals:`` block is empty
    and whose ``os_env`` is an unsandboxed ``caller_process``. Declared
    terminals stay the author's list. Any other empty block, including a
    sandbox, stays closed.

    :param spec: Loaded agent spec, or any object with the same fields.
    :returns: ``True`` when a convenience shell may be listed and launched.
    """
    if getattr(spec, "terminals", None):
        return False
    if not _unsandboxed_caller_process(getattr(spec, "os_env", None)):
        return False
    return acp_picker_identity(getattr(spec, "name", None), _harness_kind(spec))


def offers_host_shell_inventory(spec: object) -> bool:
    """Return whether the session should list the host's shells.

    Native wrappers always do. ACP pickers do only when
    :func:`acp_picker_convenience_shells_allowed` is true, so the overlay
    never replaces a spec that declared its own terminals and never opens
    a host shell for a sandboxed agent.

    :param spec: Loaded agent spec.
    :returns: ``True`` when the host inventory replaces the declared names.
    """
    from omnigent.native.native_coding_agents import native_coding_agent_for_agent_name

    if native_coding_agent_for_agent_name(getattr(spec, "name", None)) is not None:
        return True
    return acp_picker_convenience_shells_allowed(spec)


@dataclass(frozen=True)
class ConvenienceShellLaunch:
    """One ACP picker launch that must not be written onto ``spec.terminals``.

    :param available: Host shells the runner would accept, in inventory order.
    :param spec: Unsandboxed spec for the requested shell, or ``None`` when
        that name is not in :attr:`available`.
    """

    available: tuple[str, ...]
    spec: TerminalEnvSpec | None


def convenience_shell_launch(spec: object, terminal_name: str) -> ConvenienceShellLaunch | None:
    """Build the side-shell spec for an ACP picker, leaving *spec* unchanged.

    Returns ``None`` when *spec* is not an ACP picker that offers convenience
    shells. The caller then uses the declared terminal or the synthesize
    path. A picker whose requested name is not on the host returns a launch
    with ``spec is None`` so the caller can reject it.

    When the runner was not given a host inventory, the machine's installed
    interactive shells are the list. An inventory that is present but empty
    or unreadable rejects every name.

    :param spec: Loaded agent spec. Not mutated.
    :param terminal_name: Requested terminal name, e.g. ``"zsh"``.
    :returns: The launch decision, or ``None`` when this is not a convenience shell.
    """
    if not acp_picker_convenience_shells_allowed(spec):
        return None
    present, shells = _runner_shell_inventory()
    if not present:
        from omnigent._platform import installed_interactive_shells

        shells = list(installed_interactive_shells())
    if terminal_name not in shells:
        return ConvenienceShellLaunch(available=tuple(shells), spec=None)
    from omnigent.native.native_coding_agents import native_shell_terminal_specs

    return ConvenienceShellLaunch(
        available=tuple(shells),
        spec=native_shell_terminal_specs([terminal_name])[terminal_name],
    )


def _harness_kind(spec: object) -> str | None:
    """Return the executor harness id carried on *spec*, if any.

    Prefer the raw ``config.harness`` string. ``harness_kind`` falls back to
    the executor type when no harness is set, which is not a picker id.
    """
    executor = getattr(spec, "executor", None)
    if executor is None:
        return None
    config = getattr(executor, "config", None)
    if isinstance(config, dict):
        harness = config.get("harness")
        if isinstance(harness, str) and harness:
            return harness
    kind = getattr(executor, "harness_kind", None)
    if isinstance(kind, str) and kind and kind != getattr(executor, "type", None):
        return kind
    harness = getattr(executor, "harness", None)
    return harness if isinstance(harness, str) and harness else None


def _unsandboxed_caller_process(os_env: object) -> bool:
    """Return whether *os_env* is the seeded picker's unsandboxed caller process.

    ``sandbox`` must be explicitly ``none``. A missing sandbox is not treated
    as open: parsers default a missing type to the platform sandbox.
    """
    if os_env is None or getattr(os_env, "type", None) != "caller_process":
        return False
    sandbox = getattr(os_env, "sandbox", None)
    return sandbox is not None and getattr(sandbox, "type", None) == "none"


def _runner_shell_inventory() -> tuple[bool, list[str]]:
    """Return whether the host published a shell inventory, and the names."""
    from omnigent._platform import normalize_interactive_shells
    from omnigent.runner.identity import RUNNER_INTERACTIVE_SHELLS_ENV_VAR

    raw = os.environ.get(RUNNER_INTERACTIVE_SHELLS_ENV_VAR)
    if raw is None:
        return False, []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return True, []
    return True, normalize_interactive_shells(decoded)

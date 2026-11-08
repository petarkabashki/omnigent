"""Put runner processes into their own systemd user transient scopes.

An OOM kill then stays inside the runner's session instead of taking down the
host unit, and the scope gives per-runner memory/CPU accounting. Every public
function is fail-open: failures are logged and yield ``None``/``False``.

A transient scope is garbage-collected once empty, and its cgroup files vanish
with it. Final usage is therefore the last sample taken while the scope was
alive: call :func:`sample_scope_usage` periodically and
:func:`final_scope_usage` after the runner exits.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SELF_CGROUP = Path("/proc/self/cgroup")
_SYSTEMD_BUS_ARGS = (
    "org.freedesktop.systemd1",
    "/org/freedesktop/systemd1",
    "org.freedesktop.systemd1.Manager",
)
_COMMAND_TIMEOUT_SECONDS = 5.0
_PLACEMENT_WAIT_SECONDS = 2.0
_STOP_TIMEOUT_SECONDS = 5
_UNSAFE_UNIT_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

_supported: bool | None = None


@dataclass
class RunnerScope:
    """A runner's transient scope; ``last_usage`` is the newest live sample."""

    unit: str
    cgroup_dir: Path
    last_usage: ScopeUsage | None = None


@dataclass(frozen=True)
class ScopeUsage:
    """Resource usage read from a scope's cgroup files."""

    memory_peak_bytes: int | None
    memory_current_bytes: int | None
    cpu_usage_usec: int | None
    oom_kill_count: int


def scope_supported() -> bool:
    """True on Linux with cgroup v2 and a reachable systemd user manager (cached)."""
    global _supported
    if _supported is None:
        _supported = _probe_scope_support()
    return _supported


def _probe_scope_support() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if not (_CGROUP_ROOT / "cgroup.controllers").is_file():
        return False
    result = _run(
        ["busctl", "--user", "get-property", *_SYSTEMD_BUS_ARGS, "Version"],
    )
    return result is not None and result.returncode == 0


def scope_unit_name(runner_id: str) -> str:
    return f"omnigent-runner-{_UNSAFE_UNIT_CHARS.sub('_', runner_id)}.scope"


def place_runner_in_scope(
    pid: int,
    *,
    runner_id: str,
    memory_max: int | None = None,
    memory_high: int | None = None,
    cpu_quota_percent: int | None = None,
) -> RunnerScope | None:
    """Move ``pid`` (and its current descendants) into a new transient scope.

    Processes forked afterwards inherit the scope; ones that already exist do
    not follow the parent, so descendants are moved explicitly. OOMPolicy is
    ``continue`` so a kill of one process does not stop the whole scope.
    """
    try:
        if not scope_supported():
            return None
        unit = scope_unit_name(runner_id)
        pids = [pid, *_descendant_pids(pid)]
        result = _run(
            [
                "busctl",
                "--user",
                *_start_unit_args(unit, pids, memory_max, memory_high, cpu_quota_percent),
            ]
        )
        if result is None or result.returncode != 0:
            detail = (result.stderr.strip() if result else "timeout")[:200]
            logger.warning("runner scope %s not created: %s", unit, detail)
            return None
        cgroup_dir = _wait_for_cgroup_dir(pid, unit)
        if cgroup_dir is None:
            logger.warning("runner %s did not enter scope %s", pid, unit)
            return None
        _raise_oom_score_adj(pid)
        return RunnerScope(unit=unit, cgroup_dir=cgroup_dir)
    except Exception:
        logger.warning("placing runner %s in a scope failed", pid, exc_info=True)
        return None


def _start_unit_args(
    unit: str,
    pids: list[int],
    memory_max: int | None,
    memory_high: int | None,
    cpu_quota_percent: int | None,
) -> list[str]:
    properties: list[str] = [
        "PIDs", "au", str(len(pids)), *map(str, pids),
        "OOMPolicy", "s", "continue",
        "TimeoutStopUSec", "t", str(_STOP_TIMEOUT_SECONDS * 1_000_000),
        # Delegation lets terminal panes rejoin the scope (see pane_scope_join_command).
        "Delegate", "b", "true",
    ]  # fmt: skip
    count = 4
    if memory_max is not None:
        properties += ["MemoryMax", "t", str(memory_max), "MemorySwapMax", "t", "0"]
        count += 2
    if memory_high is not None:
        properties += ["MemoryHigh", "t", str(memory_high)]
        count += 1
    if cpu_quota_percent is not None:
        # CPUQuotaPerSecUSec: 100% of one CPU is 1_000_000 usec per second.
        properties += ["CPUQuotaPerSecUSec", "t", str(cpu_quota_percent * 10_000)]
        count += 1
    return [
        "call", *_SYSTEMD_BUS_ARGS, "StartTransientUnit", "ssa(sv)a(sa(sv))",
        unit, "fail", str(count), *properties, "0",
    ]  # fmt: skip


def pane_scope_join_command() -> str | None:
    """Shell prefix that moves a tmux pane's shell back into this runner's scope.

    tmux built with systemd support starts each pane in its own
    ``tmux-spawn-*.scope``, escaping the runner's memory cap and stop. ``None``
    when this process is not in a runner scope. The command fails open.
    """
    try:
        line = _SELF_CGROUP.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    unit = line.rpartition("/")[2]
    if not (unit.startswith("omnigent-runner-") and unit.endswith(".scope")):
        return None
    return _scope_join_command(unit)


def _scope_join_command(unit: str) -> str:
    busctl = shutil.which("busctl") or "busctl"
    call = [
        busctl, "--user", "call", *_SYSTEMD_BUS_ARGS,
        "AttachProcessesToUnit", "ssau", unit, "", "1",
    ]  # fmt: skip
    return f"{shlex.join(call)} $$ >/dev/null 2>&1"


def _descendant_pids(pid: int) -> list[int]:
    """All live descendants of ``pid`` from /proc (best effort)."""
    found: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        try:
            text = Path(f"/proc/{current}/task/{current}/children").read_text()
        except OSError:
            continue
        children = [int(token) for token in text.split()]
        found.extend(children)
        pending.extend(children)
    return found


def _wait_for_cgroup_dir(pid: int, unit: str) -> Path | None:
    """Wait until ``pid`` is in the scope's cgroup (the move is asynchronous)."""
    deadline = time.monotonic() + _PLACEMENT_WAIT_SECONDS
    while True:
        try:
            line = Path(f"/proc/{pid}/cgroup").read_text().splitlines()[0]
        except (OSError, IndexError):
            return None
        relative = line.partition("::")[2]
        if relative.endswith("/" + unit):
            return _CGROUP_ROOT / relative.lstrip("/")
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _raise_oom_score_adj(pid: int) -> None:
    """Undo a negative inherited oom_score_adj; raising needs no privilege."""
    path = Path(f"/proc/{pid}/oom_score_adj")
    try:
        if int(path.read_text()) < 0:
            path.write_text("0")
    except (OSError, ValueError):
        logger.debug("could not raise oom_score_adj for %s", pid, exc_info=True)


def read_scope_usage(scope: RunnerScope) -> ScopeUsage | None:
    """Read usage from the scope's cgroup files; None once the scope is gone."""
    try:
        events = _parse_flat_keyed(scope.cgroup_dir / "memory.events")
        cpu = _parse_flat_keyed(scope.cgroup_dir / "cpu.stat")
        return ScopeUsage(
            memory_peak_bytes=_read_int(scope.cgroup_dir / "memory.peak"),
            memory_current_bytes=_read_int(scope.cgroup_dir / "memory.current"),
            cpu_usage_usec=cpu.get("usage_usec"),
            oom_kill_count=events.get("oom_kill", 0),
        )
    except OSError:
        return None
    except Exception:
        logger.warning("reading usage of %s failed", scope.unit, exc_info=True)
        return None


def sample_scope_usage(scope: RunnerScope) -> ScopeUsage | None:
    """Read usage and remember it as the scope's last live sample."""
    usage = read_scope_usage(scope)
    if usage is not None:
        scope.last_usage = usage
    return usage


def final_scope_usage(scope: RunnerScope) -> ScopeUsage | None:
    """Newest usage: a live read if the scope still exists, else the last sample."""
    return sample_scope_usage(scope) or scope.last_usage


def oom_kill_count(scope: RunnerScope) -> int | None:
    """Cumulative OOM kills in the scope, or None if it cannot be read."""
    usage = read_scope_usage(scope)
    return None if usage is None else usage.oom_kill_count


def stop_scope(scope: RunnerScope) -> bool:
    """Stop the unit, killing every process in it. True if the command succeeded."""
    result = _run(
        ["systemctl", "--user", "stop", scope.unit],
        timeout=_STOP_TIMEOUT_SECONDS + _COMMAND_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0:
        detail = (result.stderr.strip() if result else "timeout")[:200]
        logger.warning("stopping scope %s failed: %s", scope.unit, detail)
        return False
    return True


def _parse_flat_keyed(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            values[key] = int(value)
    return values


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def _run(
    argv: list[str], *, timeout: float = _COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ},
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("command failed: %s", argv[:3], exc_info=True)
        return None

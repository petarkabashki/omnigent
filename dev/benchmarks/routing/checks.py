"""The step ``check`` contract: what a check is, how it runs, and what its result means.

A check is an argv run in a workspace, identified by the content hash of its canonical
JSON. Exit codes outside the declared pass and fail sets, timeouts and launch failures
are ``error``: the check gave no answer, so they never count as a failed step.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

CHECK_SCHEMA_VERSION = 1
MAX_TIMEOUT_S = 3600
QUALIFY_REPEATS = 3
# Checks must not read the owner's credentials or the session's Omnigent environment.
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


class CheckSpecError(ValueError):
    """A check definition breaks the contract."""


def _relative_path(value: Any, name: str) -> str:  # type: ignore[explicit-any]  # parsed JSON
    path = PurePosixPath(value) if isinstance(value, str) and value else None
    if path is None or path.is_absolute() or ".." in path.parts:
        raise CheckSpecError(f"{name} must be a relative path inside the workspace: {value!r}")
    return str(path)


@dataclass(frozen=True)
class StepCheck:
    """One step's check.

    :param argv: Command, run without a shell.
    :param cwd: Working directory, relative to the workspace root.
    :param timeout_s: Wall-clock limit; exceeding it is an ``error``.
    :param protected_paths: Workspace paths restored from the pinned commit before the
        check runs, so an agent cannot pass by editing the test.
    :param pass_exit_codes: Exit codes meaning the step passed.
    :param fail_exit_codes: Exit codes meaning the step failed.
    """

    argv: tuple[str, ...]
    cwd: str = "."
    timeout_s: int = 600
    protected_paths: tuple[str, ...] = ()
    pass_exit_codes: tuple[int, ...] = (0,)
    fail_exit_codes: tuple[int, ...] = (1,)

    def __post_init__(self) -> None:
        if not self.argv or not all(isinstance(part, str) and part for part in self.argv):
            raise CheckSpecError("argv must be a non-empty list of non-empty strings")
        _relative_path(self.cwd, "cwd")
        for path in self.protected_paths:
            _relative_path(path, "protected_paths entry")
        if not 1 <= self.timeout_s <= MAX_TIMEOUT_S:
            raise CheckSpecError(f"timeout_s must be between 1 and {MAX_TIMEOUT_S}")
        if not self.pass_exit_codes or set(self.pass_exit_codes) & set(self.fail_exit_codes):
            raise CheckSpecError("pass_exit_codes must be non-empty and disjoint from fail codes")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepCheck:  # type: ignore[explicit-any]  # parsed JSON
        """Parse a stored spec, rejecting unknown schema versions and fields."""
        fields = dict(data)
        version = fields.pop("schema_version", None)
        if version != CHECK_SCHEMA_VERSION:
            raise CheckSpecError(f"unsupported check schema_version: {version!r}")
        try:
            return cls(
                **{
                    name: tuple(value) if isinstance(value, list) else value
                    for name, value in fields.items()
                }
            )
        except TypeError as error:
            raise CheckSpecError(str(error)) from error

    def to_bytes(self) -> bytes:
        """Canonical JSON: the bytes whose hash is the ``check_ref``."""
        spec = {"schema_version": CHECK_SCHEMA_VERSION, **asdict(self)}
        return json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()

    @property
    def check_ref(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True)
class CheckResult:
    """``status`` is ``passed``, ``failed`` or ``error``; ``error`` has no pass/fail answer."""

    status: str
    exit_code: int | None
    duration_s: float
    detail: str = ""

    @property
    def passed(self) -> bool | None:
        return {"passed": True, "failed": False}.get(self.status)


def run_check(check: StepCheck, workspace: Path) -> CheckResult:
    """Run a check in ``workspace``; never raises for a misbehaving command."""
    started = time.monotonic()

    def result(status: str, exit_code: int | None = None, detail: str = "") -> CheckResult:
        return CheckResult(status, exit_code, round(time.monotonic() - started, 3), detail)

    env = {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}
    try:
        process = subprocess.Popen(
            check.argv,
            cwd=workspace / check.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        return result("error", detail=f"could not start: {error}")
    try:
        exit_code = process.wait(timeout=check.timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return result("error", detail="timed out")
    if exit_code in check.pass_exit_codes:
        return result("passed", exit_code)
    if exit_code in check.fail_exit_codes:
        return result("failed", exit_code)
    return result("error", exit_code, "exit code outside the declared pass and fail sets")


def qualify_check(check: StepCheck, workspace: Path, repeats: int = QUALIFY_REPEATS) -> str:
    """Classify a check against the unchanged base tree, before any money is spent.

    Returns ``qualified`` (fails every time, so passing later means the agent did the
    work), ``vacuous`` (passes with no work done), ``flaky`` or ``broken``.
    """
    statuses = {run_check(check, workspace).status for _ in range(repeats)}
    if "error" in statuses:
        return "broken"
    if statuses == {"failed"}:
        return "qualified"
    return "vacuous" if statuses == {"passed"} else "flaky"

#!/usr/bin/env python3
"""Offline paired replay: run each eligible task under both choices, score it by its check.

Usage:
    uv run --no-sync python -m dev.benchmarks.routing.replay preflight \\
        --decisions decisions.jsonl --outcomes outcomes.jsonl \\
        --repo . --blobs blobs/ --output preflight.jsonl

``preflight`` spends nothing on models: it resolves every bundle and qualifies its check.
There is no live executor yet, so ``run_replay`` is only reachable from tests. The rows
it writes are the input of ``eval verdict``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from dev.benchmarks.routing.checks import (
    CheckResult,
    StepCheck,
    qualify_check,
    run_check,
)
from dev.benchmarks.routing.eval import DecisionRow, build_dataset, read_jsonl

ARMS = ("baseline", "router")
_BLOB_REF = re.compile(r"sha256:[0-9a-f]{64}")


class BundleError(ValueError):
    """A replay bundle reference cannot be resolved."""


class BlobStore:
    """Content-addressed files under a directory; stands in until L7 picks the real store."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, ref: str) -> Path:
        if not _BLOB_REF.fullmatch(ref):
            raise BundleError(f"not a sha256 blob ref: {ref!r}")
        digest = ref.removeprefix("sha256:")
        return self.root / digest[:2] / digest

    def write(self, data: bytes) -> str:
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600)
        path.write_bytes(data)
        return ref

    def read(self, ref: str) -> bytes:
        try:
            data = self._path(ref).read_bytes()
        except FileNotFoundError:
            raise BundleError(f"blob missing: {ref}") from None
        if "sha256:" + hashlib.sha256(data).hexdigest() != ref:
            raise BundleError(f"blob does not match its hash: {ref}")
        return data

    def read_check(self, ref: str) -> StepCheck:
        try:
            return StepCheck.from_dict(json.loads(self.read(ref)))
        except (ValueError, TypeError) as error:
            raise BundleError(f"invalid check {ref}: {error}") from error


@dataclass(frozen=True)
class Choice:
    """What an arm runs on. Replays compare choices, so equal choices are one policy."""

    agent_id: str | None = None
    harness: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    role: str | None = None
    executor: str | None = None


@dataclass(frozen=True)
class ReplayTask:
    task_id: str
    input_ref: str
    git_commit: str
    check_ref: str
    baseline: Choice
    router: Choice


def tasks_from_rows(rows: Iterable[DecisionRow], router_backend: str) -> list[ReplayTask]:
    """Offline-eligible decisions the backend proposed for; a proposal overlays the actual."""
    tasks = []
    names = Choice.__dataclass_fields__
    for row in rows:
        proposal = row.proposals.get(router_backend)
        if proposal is None or not row.offline_eligible:
            continue
        baseline = Choice(**{name: row.actual.get(name) for name in names})
        overlay = {name: proposal[name] for name in names if proposal.get(name) is not None}
        # offline_eligible guarantees the three references are set.
        tasks.append(
            ReplayTask(
                row.decision_id,
                str(row.input_ref),
                str(row.git_commit),
                str(row.check_ref),
                baseline,
                replace(baseline, **overlay),
            )
        )
    return tasks


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(workspace), *args], check=True, capture_output=True, timeout=300
    )


@contextmanager
def checkout(repo: Path, commit: str) -> Iterator[Path]:
    """A throwaway clone at ``commit``, leaving the source repo's worktree list alone."""
    with tempfile.TemporaryDirectory(prefix="routing-replay-") as parent:
        workspace = Path(parent) / "workspace"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(repo), str(workspace)],
            check=True,
            capture_output=True,
            timeout=300,
        )
        _git(workspace, "checkout", "--quiet", "--detach", commit)
        yield workspace


def preflight_task(task: ReplayTask, repo: Path, blobs: BlobStore) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON row
    """Resolve a task's bundle and qualify its check. Runs the check, never an agent."""
    report: dict[str, Any] = {"task_id": task.task_id, "check_ref": task.check_ref}  # type: ignore[explicit-any]  # JSON row
    try:
        blobs.read(task.input_ref)
        check = blobs.read_check(task.check_ref)
    except BundleError as error:
        return {**report, "status": "unresolved", "detail": str(error)}
    try:
        with checkout(repo, task.git_commit) as workspace:
            return {**report, "status": qualify_check(check, workspace)}
    except subprocess.CalledProcessError:
        return {**report, "status": "unresolved", "detail": f"no commit {task.git_commit}"}


@dataclass(frozen=True)
class AgentRun:
    """One attempt. ``infra_error`` means the host failed, not the agent or its choice."""

    usd: float
    infra_error: bool = False


class ReplayExecutor(Protocol):
    def run(self, prompt: bytes, choice: Choice, workspace: Path) -> AgentRun: ...


@dataclass(frozen=True)
class ReplaySummary:
    tasks_finished: int
    usd_spent: float
    stopped_by_budget: bool


def _score(task: ReplayTask, check: StepCheck, workspace: Path) -> CheckResult:
    if check.protected_paths:
        try:
            _git(workspace, "checkout", "--quiet", task.git_commit, "--", *check.protected_paths)
        except subprocess.CalledProcessError:
            return CheckResult("error", None, 0.0, "a protected path is not in the commit")
    return run_check(check, workspace)


def _run_arm(  # type: ignore[explicit-any]  # JSON row
    task: ReplayTask,
    choice: Choice,
    check: StepCheck,
    prompt: bytes,
    repo: Path,
    executor: ReplayExecutor,
    max_attempts: int,
) -> dict[str, Any]:
    """Run until one attempt survives; failed attempts stay on the bill for this decision."""
    usd = 0.0
    for attempts in range(1, max_attempts + 1):
        with checkout(repo, task.git_commit) as workspace:
            run = executor.run(prompt, choice, workspace)
            usd += run.usd
            if run.infra_error:
                continue
            result = _score(task, check, workspace)
            return {
                "passed": result.passed,
                "check_status": result.status,
                "usd": usd,
                "attempts": attempts,
            }
    return {"passed": None, "check_status": "not_run", "usd": usd, "attempts": max_attempts}


def _spent(rows: Iterable[dict[str, Any]]) -> float:  # type: ignore[explicit-any]  # JSON rows
    return sum(
        float(row["usd"]) for row in rows if not (row.get("shared_run") and row["arm"] == "router")
    )


def run_replay(
    tasks: Sequence[ReplayTask],
    repo: Path,
    blobs: BlobStore,
    executor: ReplayExecutor,
    results_path: Path,
    max_usd: float,
    max_attempts: int = 2,
    seed: int = 0,
) -> ReplaySummary:
    """Replay tasks in pairs, appending one row per arm so an interrupted run resumes.

    The budget is checked between tasks, keeping pairs whole, so a run can pass
    ``max_usd`` by at most one task. A task whose choices are equal runs once and is
    recorded for both arms, because any difference between two runs would only be noise.
    """
    existing = read_jsonl(results_path) if results_path.exists() else []
    finished = {(str(row["task_id"]), row["arm"]) for row in existing}
    spent = _spent(existing)
    done = 0
    with results_path.open("a", encoding="utf-8") as results:

        def record(task: ReplayTask, arm: str, row: dict[str, Any]) -> None:  # type: ignore[explicit-any]  # JSON row
            line = {"task_id": task.task_id, "arm": arm, "check_ref": task.check_ref, **row}
            results.write(json.dumps(line, sort_keys=True) + "\n")
            results.flush()

        for task in tasks:
            pending = [arm for arm in ARMS if (task.task_id, arm) not in finished]
            if not pending:
                done += 1
                continue
            if spent >= max_usd:
                return ReplaySummary(done, spent, True)
            prompt = blobs.read(task.input_ref)
            check = blobs.read_check(task.check_ref)
            if task.baseline == task.router:
                row = _run_arm(task, task.baseline, check, prompt, repo, executor, max_attempts)
                spent += float(row["usd"])
                for arm in ARMS:
                    record(task, arm, {**row, "shared_run": True})
            else:
                # Random arm order, so neither arm always gets the earlier, quieter slot.
                random.Random(f"{seed}:{task.task_id}").shuffle(pending)
                for arm in pending:
                    choice = task.baseline if arm == "baseline" else task.router
                    row = _run_arm(task, choice, check, prompt, repo, executor, max_attempts)
                    spent += float(row["usd"])
                    record(task, arm, row)
            done += 1
    return ReplaySummary(done, spent, False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--decisions", type=Path, required=True)
    preflight.add_argument("--outcomes", type=Path)
    preflight.add_argument("--repo", type=Path, required=True)
    preflight.add_argument("--blobs", type=Path, required=True)
    preflight.add_argument("--router-backend", default="smart")
    preflight.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = build_dataset(
        read_jsonl(args.decisions), read_jsonl(args.outcomes) if args.outcomes else ()
    )
    blobs = BlobStore(args.blobs)
    counts: dict[str, int] = {}
    with args.output.open("w", encoding="utf-8") as output:
        for task in tasks_from_rows(rows, args.router_backend):
            report = preflight_task(task, args.repo, blobs)
            counts[report["status"]] = counts.get(report["status"], 0) + 1
            output.write(json.dumps({**report, "choices": asdict(task.router)}) + "\n")
    json.dump(counts, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

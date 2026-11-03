from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dev.benchmarks.routing.checks import (
    CheckSpecError,
    StepCheck,
    qualify_check,
    run_check,
)
from dev.benchmarks.routing.eval import (
    build_dataset,
    coverage_by_site,
    offline_verdict,
    read_jsonl,
)
from dev.benchmarks.routing.replay import (
    AgentRun,
    BlobStore,
    BundleError,
    Choice,
    ReplayTask,
    main,
    preflight_task,
    run_replay,
    tasks_from_rows,
)

PYTHON = sys.executable
# Passes once the agent has created done.txt.
DONE_CHECK = "import os, sys; sys.exit(0 if os.path.exists('done.txt') else 1)"


def _check(source: str, **fields) -> StepCheck:
    return StepCheck(argv=(PYTHON, "-c", source), **fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"argv": ()},
        {"argv": ("x",), "cwd": "../up"},
        {"argv": ("x",), "cwd": "/abs"},
        {"argv": ("x",), "protected_paths": ("../secret",)},
        {"argv": ("x",), "timeout_s": 0},
        {"argv": ("x",), "pass_exit_codes": (0,), "fail_exit_codes": (0,)},
    ],
)
def test_check_spec_rejects_contract_violations(fields) -> None:
    with pytest.raises(CheckSpecError):
        StepCheck(**fields)


def test_check_ref_round_trips_and_tracks_content() -> None:
    check = StepCheck(argv=("pytest", "tests/x.py"), protected_paths=("tests",))
    assert StepCheck.from_dict(json.loads(check.to_bytes())) == check
    assert check.check_ref != StepCheck(argv=("pytest", "tests/y.py")).check_ref


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": 2, "argv": ["x"]},
        {"argv": ["x"]},
        {"schema_version": 1, "argv": ["x"], "shell": True},
    ],
)
def test_check_spec_rejects_unknown_shapes(stored) -> None:
    with pytest.raises(CheckSpecError):
        StepCheck.from_dict(stored)


@pytest.mark.parametrize(
    ("check", "status", "passed"),
    [
        (_check("raise SystemExit(0)"), "passed", True),
        (_check("raise SystemExit(1)"), "failed", False),
        (_check("raise SystemExit(5)"), "error", None),
        (StepCheck(argv=("/no/such/binary",)), "error", None),
    ],
)
def test_run_check_only_declared_codes_answer(tmp_path, check, status, passed) -> None:
    result = run_check(check, tmp_path)
    assert (result.status, result.passed) == (status, passed)


def test_run_check_timeout_is_an_error_and_kills_children(tmp_path) -> None:
    source = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = run_check(_check(source, timeout_s=1), tmp_path)
    assert result.status == "error" and result.detail == "timed out"
    assert time.monotonic() - started < 30
    child_pid = int((tmp_path / "child.pid").read_text())
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the check's child outlived the timeout")


def test_run_check_hides_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    source = "import os, sys; sys.exit(1 if 'ANTHROPIC_API_KEY' in os.environ else 0)"
    assert run_check(_check(source), tmp_path).status == "passed"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("raise SystemExit(1)", "qualified"),
        ("raise SystemExit(0)", "vacuous"),
        ("raise SystemExit(3)", "broken"),
        (
            "import os, sys\n"
            "first = not os.path.exists('seen')\n"
            "open('seen', 'w').close()\n"
            "sys.exit(0 if first else 1)\n",
            "flaky",
        ),
    ],
)
def test_qualify_check_classifies_base_tree_behaviour(tmp_path, source, expected) -> None:
    assert qualify_check(_check(source), tmp_path) == expected


def test_errored_check_is_no_answer_rather_than_failure() -> None:
    decision = {"decision_id": "d1", "session_id": "s", "backend": "rules", "decision": {}}
    outcomes = [{"decision_id": "d1", "event": "check", "passed": None, "check_ref": "sha256:a"}]
    (row,) = build_dataset([decision], outcomes)
    assert row.success is None
    site = coverage_by_site([row])["session_create"]
    assert (site["with_check"], site["check_errors"]) == (1, 1)


def test_declared_check_counts_as_coverage_before_any_result() -> None:
    decision = {"decision_id": "d1", "session_id": "s", "backend": "rules", "decision": {}}
    declared = [{"decision_id": "d1", "event": "check_declared", "check_ref": "sha256:a"}]
    (row,) = build_dataset([decision], declared)
    assert coverage_by_site([row])["session_create"]["check_coverage"] == 1.0
    assert row.success is None


def test_verdict_drops_unscored_pairs_and_counts_them() -> None:
    rows = []
    for task, (baseline_passed, router_passed) in enumerate(
        [(True, True), (False, True), (None, True), (True, None)]
    ):
        rows.append({"task_id": task, "arm": "baseline", "passed": baseline_passed, "usd": 1.0})
        rows.append({"task_id": task, "arm": "router", "passed": router_passed, "usd": 1.0})
    result = offline_verdict(rows)
    assert result.pairs == 2
    assert (result.unscored_baseline, result.unscored_router) == (1, 1)
    assert result.verdict == "insufficient"


class ScriptedAgent:
    """Passes the task on ``good_models``; bills ``usd`` per run."""

    def __init__(self, good_models=("big",), usd=1.0, infra_failures=0, tamper=False) -> None:
        self.good_models, self.usd = good_models, usd
        self.infra_failures, self.tamper = infra_failures, tamper
        self.calls: list[tuple[bytes, str | None]] = []

    def run(self, prompt: bytes, choice: Choice, workspace: Path) -> AgentRun:
        self.calls.append((prompt, choice.model))
        if self.infra_failures:
            self.infra_failures -= 1
            return AgentRun(self.usd, infra_error=True)
        if self.tamper:
            (workspace / "check.py").write_text("raise SystemExit(0)\n")
        if choice.model in self.good_models:
            (workspace / "done.txt").write_text("done")
        return AgentRun(self.usd)


@pytest.fixture
def bench(tmp_path):
    """A one-commit repo whose check.py passes once done.txt exists, plus its blobs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "check.py").write_text(DONE_CHECK)
    git = ["git", "-C", str(repo), "-c", "user.name=b", "-c", "user.email=b@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "check.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    commit = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    blobs = BlobStore(tmp_path / "blobs")
    check = StepCheck(argv=(PYTHON, "check.py"), protected_paths=("check.py",))
    return {
        "repo": repo,
        "commit": commit,
        "blobs": blobs,
        "input_ref": blobs.write(b"do the task"),
        "check_ref": blobs.write(check.to_bytes()),
        "results": tmp_path / "results.jsonl",
    }


def _task(bench, task_id="t1", baseline="big", router="small", **fields) -> ReplayTask:
    values = {
        "input_ref": bench["input_ref"],
        "git_commit": bench["commit"],
        "check_ref": bench["check_ref"],
        **fields,
    }
    return ReplayTask(
        task_id, baseline=Choice(model=baseline), router=Choice(model=router), **values
    )


def _replay(bench, tasks, agent, max_usd=100.0, **options):
    return run_replay(
        tasks, bench["repo"], bench["blobs"], agent, bench["results"], max_usd, **options
    )


def test_blob_store_detects_tampering_and_missing_blobs(bench) -> None:
    blobs = bench["blobs"]
    assert blobs.read(bench["input_ref"]) == b"do the task"
    digest = bench["input_ref"].removeprefix("sha256:")
    (blobs.root / digest[:2] / digest).write_bytes(b"changed")
    with pytest.raises(BundleError):
        blobs.read(bench["input_ref"])
    with pytest.raises(BundleError):
        blobs.read("sha256:" + "0" * 64)
    with pytest.raises(BundleError):
        blobs.read("../../etc/passwd")


def test_tasks_overlay_proposal_and_skip_ineligible() -> None:
    bundle = {"check_ref": "c", "input_ref": "i", "git_commit": "g"}
    actual = {"model": "big", "harness": "claude"}
    decisions = [
        {
            "decision_id": "ok",
            "backend": "smart",
            "actual": actual,
            "decision": {"model": "small"},
            **bundle,
        },
        {
            "decision_id": "pinned",
            "backend": "smart",
            "actual": actual,
            "decision": {},
            "pinned": ["model"],
            **bundle,
        },
        {"decision_id": "no-bundle", "backend": "smart", "actual": actual, "decision": {}},
        {"decision_id": "other", "backend": "rules", "actual": actual, "decision": {}, **bundle},
    ]
    (task,) = tasks_from_rows(build_dataset(decisions), "smart")
    assert task.task_id == "ok"
    assert task.baseline == Choice(model="big", harness="claude")
    assert task.router == Choice(model="small", harness="claude")


def test_preflight_reports_each_bundle_state(bench) -> None:
    repo, blobs = bench["repo"], bench["blobs"]
    assert preflight_task(_task(bench), repo, blobs)["status"] == "qualified"
    vacuous_ref = blobs.write(_check("raise SystemExit(0)").to_bytes())
    assert preflight_task(_task(bench, check_ref=vacuous_ref), repo, blobs)["status"] == "vacuous"
    for broken in (
        _task(bench, input_ref="sha256:" + "0" * 64),
        _task(bench, check_ref=blobs.write(b"not json")),
        _task(bench, git_commit="0" * 40),
    ):
        assert preflight_task(broken, repo, blobs)["status"] == "unresolved"


def test_replay_rows_feed_the_verdict(bench) -> None:
    agent = ScriptedAgent(good_models=("small",))
    summary = _replay(bench, [_task(bench, f"t{i}") for i in range(3)], agent)
    assert (summary.tasks_finished, summary.usd_spent) == (3, 6.0)
    assert {prompt for prompt, _ in agent.calls} == {b"do the task"}
    verdict = offline_verdict(read_jsonl(bench["results"]))
    assert (verdict.pairs, verdict.discordant_router_only, verdict.verdict) == (
        3,
        3,
        "insufficient",
    )


def test_interrupted_replay_resumes_without_rerunning(bench) -> None:
    tasks = [_task(bench, "t1"), _task(bench, "t2")]
    _replay(bench, tasks[:1], ScriptedAgent())
    lines = bench["results"].read_text().splitlines()
    bench["results"].write_text(lines[0] + "\n")
    agent = ScriptedAgent()
    summary = _replay(bench, tasks, agent)
    assert len(agent.calls) == 3
    assert summary.usd_spent == 4.0
    assert {(row["task_id"], row["arm"]) for row in read_jsonl(bench["results"])} == {
        (task, arm) for task in ("t1", "t2") for arm in ("baseline", "router")
    }


def test_budget_stops_between_tasks_so_pairs_stay_whole(bench) -> None:
    tasks = [_task(bench, f"t{i}") for i in range(3)]
    summary = _replay(bench, tasks, ScriptedAgent(), max_usd=2.0)
    assert summary.stopped_by_budget
    assert (summary.tasks_finished, summary.usd_spent) == (1, 2.0)
    assert len(read_jsonl(bench["results"])) == 2


def test_equal_choices_run_once_and_bill_once(bench) -> None:
    agent = ScriptedAgent()
    summary = _replay(bench, [_task(bench, router="big")], agent)
    assert len(agent.calls) == 1
    assert summary.usd_spent == 1.0
    rows = read_jsonl(bench["results"])
    assert sorted(row["arm"] for row in rows) == ["baseline", "router"]
    assert {row["passed"] for row in rows} == {True}
    assert _replay(bench, [_task(bench, router="big")], agent).usd_spent == 1.0


def test_infra_retry_is_charged_to_the_decision(bench) -> None:
    agent = ScriptedAgent(infra_failures=1)
    _replay(bench, [_task(bench, router="big")], agent)
    (row, _) = read_jsonl(bench["results"])
    assert (row["usd"], row["attempts"], row["passed"]) == (2.0, 2, True)


def test_exhausted_infra_retries_leave_the_arm_unscored(bench) -> None:
    _replay(bench, [_task(bench, router="big")], ScriptedAgent(infra_failures=2))
    (row, _) = read_jsonl(bench["results"])
    assert (row["passed"], row["check_status"], row["usd"]) == (None, "not_run", 2.0)


def test_agent_cannot_pass_by_editing_a_protected_check(bench) -> None:
    _replay(bench, [_task(bench)], ScriptedAgent(good_models=(), tamper=True))
    assert {row["passed"] for row in read_jsonl(bench["results"])} == {False}


def test_preflight_cli_summarises_counts(bench, tmp_path, capsys) -> None:
    record = {
        "decision_id": "d1",
        "backend": "smart",
        "actual": {"model": "big"},
        "decision": {"model": "small"},
        "input_ref": bench["input_ref"],
        "git_commit": bench["commit"],
        "check_ref": bench["check_ref"],
    }
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(json.dumps(record) + "\n")
    output = tmp_path / "preflight.jsonl"
    arguments = ["preflight", "--decisions", str(decisions), "--repo", str(bench["repo"])]
    arguments += ["--blobs", str(bench["blobs"].root), "--output", str(output)]
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {"qualified": 1}
    assert read_jsonl(output)[0]["status"] == "qualified"

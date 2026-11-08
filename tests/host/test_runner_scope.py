"""Tests for omnigent.host.runner_scope."""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from omnigent.host import runner_scope
from omnigent.host.runner_scope import RunnerScope, ScopeUsage


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


@pytest.fixture(autouse=True)
def _reset_support_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_scope, "_supported", None)


def test_unit_name_is_sanitized() -> None:
    assert runner_scope.scope_unit_name("runner/a b:1") == "omnigent-runner-runner_a_b_1.scope"


def test_start_unit_args_with_all_limits() -> None:
    argv = runner_scope._start_unit_args("u.scope", [10, 11], 1000, 500, 150)
    assert argv[:4] == [
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    ]
    assert argv[4:8] == ["StartTransientUnit", "ssa(sv)a(sa(sv))", "u.scope", "fail"]
    # PIDs, OOMPolicy, TimeoutStopUSec, Delegate, MemoryMax, MemorySwapMax, MemoryHigh, CPU
    assert argv[8] == "8"
    assert argv[9:14] == ["PIDs", "au", "2", "10", "11"]
    assert argv[argv.index("MemoryMax") :][:3] == ["MemoryMax", "t", "1000"]
    assert argv[argv.index("CPUQuotaPerSecUSec") :][:3] == ["CPUQuotaPerSecUSec", "t", "1500000"]
    assert argv[-1] == "0"


def test_start_unit_args_without_limits_still_isolates() -> None:
    argv = runner_scope._start_unit_args("u.scope", [10], None, None, None)
    assert argv[8] == "4"
    assert argv[argv.index("Delegate") :][:3] == ["Delegate", "b", "true"]
    assert "MemoryMax" not in argv
    assert argv[argv.index("OOMPolicy") :][:3] == ["OOMPolicy", "s", "continue"]


@pytest.mark.parametrize(
    ("cgroup_line", "unit"),
    [
        ("0::/user.slice/app.slice/omnigent-runner-tok_1.scope", "omnigent-runner-tok_1.scope"),
        ("0::/user.slice/app.slice/tmux-spawn-1234.scope", None),
        ("0::/user.slice/session-4.scope", None),
        ("", None),
    ],
)
def test_pane_scope_join_command_only_inside_runner_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cgroup_line: str, unit: str | None
) -> None:
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text(cgroup_line + "\n" if cgroup_line else "")
    monkeypatch.setattr(runner_scope, "_SELF_CGROUP", cgroup_file)
    command = runner_scope.pane_scope_join_command()
    if unit is None:
        assert command is None
        return
    assert command is not None
    assert f"AttachProcessesToUnit ssau {unit} '' 1 $$" in command
    assert command.endswith(">/dev/null 2>&1")


@pytest.mark.parametrize(
    ("cgroup_line", "expected"),
    [
        ("0::/app.slice/omnigent-runner-tok.scope", 3),
        ("0::/app.slice/tmux-spawn-1.scope", 0),
        ("0::/app.slice/omnigent-runner-gone.scope", 0),
    ],
)
def test_own_scope_oom_kills_reads_only_a_runner_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cgroup_line: str, expected: int
) -> None:
    for unit in ("omnigent-runner-tok.scope", "tmux-spawn-1.scope"):
        (tmp_path / "app.slice" / unit).mkdir(parents=True)
        (tmp_path / "app.slice" / unit / "memory.events").write_text("oom 3\noom_kill 3\n")
        (tmp_path / "app.slice" / unit / "cpu.stat").write_text("usage_usec 1\n")
        for name in ("memory.peak", "memory.current"):
            (tmp_path / "app.slice" / unit / name).write_text("1\n")
    cgroup_file = tmp_path / "self-cgroup"
    cgroup_file.write_text(cgroup_line + "\n")
    monkeypatch.setattr(runner_scope, "_SELF_CGROUP", cgroup_file)
    monkeypatch.setattr(runner_scope, "_CGROUP_ROOT", tmp_path)
    assert runner_scope.own_scope_oom_kills() == expected


def test_read_scope_usage_parses_cgroup_files(tmp_path: Path) -> None:
    (tmp_path / "memory.events").write_text("low 0\nhigh 3\nmax 9\noom 2\noom_kill 2\n")
    (tmp_path / "memory.peak").write_text("5242880\n")
    (tmp_path / "memory.current").write_text("1048576\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 777\nuser_usec 700\nsystem_usec 77\n")
    scope = RunnerScope(unit="u.scope", cgroup_dir=tmp_path)
    assert runner_scope.read_scope_usage(scope) == ScopeUsage(5242880, 1048576, 777, 2)
    assert runner_scope.oom_kill_count(scope) == 2


def test_read_scope_usage_none_when_scope_gone(tmp_path: Path) -> None:
    scope = RunnerScope(unit="u.scope", cgroup_dir=tmp_path / "gone")
    assert runner_scope.read_scope_usage(scope) is None
    assert runner_scope.oom_kill_count(scope) is None


def test_final_usage_falls_back_to_last_sample(tmp_path: Path) -> None:
    for name, text in {
        "memory.events": "oom_kill 1\n",
        "memory.peak": "42\n",
        "memory.current": "40\n",
        "cpu.stat": "usage_usec 5\n",
    }.items():
        (tmp_path / name).write_text(text)
    scope = RunnerScope(unit="u.scope", cgroup_dir=tmp_path)
    sampled = runner_scope.sample_scope_usage(scope)
    assert sampled is not None and sampled.oom_kill_count == 1
    for child in tmp_path.iterdir():
        child.unlink()
    tmp_path.rmdir()  # scope garbage-collected after the runner exits
    assert runner_scope.final_scope_usage(scope) == sampled


def test_place_returns_none_when_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_scope, "_supported", False)
    assert runner_scope.place_runner_in_scope(1, runner_id="r") is None


def test_place_fails_open_when_busctl_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_scope, "_supported", True)
    monkeypatch.setattr(runner_scope, "_descendant_pids", lambda pid: [])
    monkeypatch.setattr(runner_scope, "_run", lambda argv, **kw: _completed(1, "denied"))
    assert runner_scope.place_runner_in_scope(1, runner_id="r") is None


def test_place_fails_open_when_command_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_scope, "_supported", True)
    monkeypatch.setattr(runner_scope, "_descendant_pids", lambda pid: [])
    monkeypatch.setattr(runner_scope, "_run", lambda argv, **kw: None)
    assert runner_scope.place_runner_in_scope(1, runner_id="r") is None


def test_place_fails_open_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_scope, "_supported", True)

    def boom(pid: int) -> list[int]:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_scope, "_descendant_pids", boom)
    assert runner_scope.place_runner_in_scope(1, runner_id="r") is None


def test_place_passes_descendants_and_reports_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runner_scope, "_supported", True)
    monkeypatch.setattr(runner_scope, "_descendant_pids", lambda pid: [22, 33])
    monkeypatch.setattr(
        runner_scope, "_run", lambda argv, **kw: calls.append(argv) or _completed()
    )
    monkeypatch.setattr(runner_scope, "_wait_for_cgroup_dir", lambda pid, unit: tmp_path)
    monkeypatch.setattr(runner_scope, "_raise_oom_score_adj", lambda pid: None)
    scope = runner_scope.place_runner_in_scope(11, runner_id="abc", memory_max=1)
    assert scope == RunnerScope(unit="omnigent-runner-abc.scope", cgroup_dir=tmp_path)
    assert calls[0][:2] == ["busctl", "--user"]
    assert "11" in calls[0] and "22" in calls[0] and "33" in calls[0]


def test_stop_scope_reports_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = RunnerScope(unit="u.scope", cgroup_dir=tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(runner_scope, "_run", lambda argv, **kw: seen.append(argv) or _completed())
    assert runner_scope.stop_scope(scope) is True
    assert seen == [["systemctl", "--user", "stop", "u.scope"]]
    monkeypatch.setattr(runner_scope, "_run", lambda argv, **kw: None)
    assert runner_scope.stop_scope(scope) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs systemd on Linux")
def test_real_scope_contains_oom_to_child_and_stop_kills_all() -> None:
    if not runner_scope.scope_supported():
        pytest.skip("no systemd user manager with cgroup v2")
    runner_id = f"pytest-{uuid.uuid4().hex[:8]}"
    allocate = "b=bytearray(300*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1"
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess,sys,time;"
            f"time.sleep(1);p=subprocess.run([sys.executable,'-c',{allocate!r}]);"
            f"print(p.returncode,flush=True);time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    scope = runner_scope.place_runner_in_scope(
        runner.pid, runner_id=runner_id, memory_max=50 * 1024 * 1024
    )
    try:
        if scope is None:
            pytest.skip("user manager refused the scope")
        assert runner.stdout is not None
        assert runner.stdout.readline().strip() == "-9"
        assert runner.poll() is None  # only the child was killed
        assert runner_scope.oom_kill_count(scope) == 1
        # A pane started outside the scope (as tmux does) rejoins it and stops with it.
        pane = subprocess.Popen(
            ["sh", "-c", f"{runner_scope._scope_join_command(scope.unit)}; exec sleep 30"]
        )
        assert runner_scope._wait_for_cgroup_dir(pane.pid, scope.unit) == scope.cgroup_dir
        assert runner_scope.stop_scope(scope)
        assert runner.wait(timeout=10) is not None
        assert pane.wait(timeout=10) is not None
        deadline = time.monotonic() + 5
        while scope.cgroup_dir.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert runner_scope.read_scope_usage(scope) is None
    finally:
        runner.kill()
        runner.wait()
        subprocess.run(
            ["systemctl", "--user", "reset-failed", scope.unit if scope else "x.scope"],
            capture_output=True,
            check=False,
        )

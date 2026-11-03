"""Executor profiles: loading, validation, mtime cache, fill-only-unset precedence, role pick."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.executor_profiles import (
    ExecutorProfile,
    apply_executor_profile,
    executors_path,
    get_executor_profile,
    load_executor_profiles,
    parse_executors,
)
from omnigent.runtime.role_instructions import role_executor_name
from omnigent.server.schemas import ProjectSessionCreateRequest


def _write_executors(home: Path, text: str) -> Path:
    path = executors_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _write_role(root: Path, name: str, text: str) -> None:
    path = root / ".omnigent" / "roles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_loads_all_profile_fields(tmp_path: Path) -> None:
    _write_executors(
        tmp_path,
        "deep:\n  agent_id: ag_1\n  harness: claude-native\n  model: opus\n"
        "  reasoning_effort: high\n  host_needs: [gpu, big-disk]\n"
        "  host_preference: hpz4\n  description: Deep work\nplain: {}\nempty:\n",
    )
    profiles = load_executor_profiles(tmp_path)
    assert set(profiles) == {"deep", "plain", "empty"}
    assert profiles["deep"] == ExecutorProfile(
        name="deep",
        agent_id="ag_1",
        harness="claude-native",
        model="opus",
        reasoning_effort="high",
        host_needs=("gpu", "big-disk"),
        host_preference="hpz4",
        description="Deep work",
    )


def test_missing_file_means_no_profiles(tmp_path: Path) -> None:
    assert load_executor_profiles(tmp_path) == {}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a:\n  harnes: pi\n", "unknown keys ['harnes']"),
        ("9bad:\n  harness: pi\n", "must match"),
        ("a:\n  model: 3\n", "model must be a nonempty string"),
        ("a:\n  harness: ''\n", "harness must be a nonempty string"),
        ("a:\n  host_needs: gpu\n", "host_needs must be a list"),
        ("a: pi\n", "must be a mapping"),
        ("- a\n", "top level must map"),
        ("a: [unclosed\n", "unreadable YAML"),
    ],
)
def test_invalid_files_raise_clear_errors(text: str, fragment: str, tmp_path: Path) -> None:
    with pytest.raises(OmnigentError) as caught:
        parse_executors(text, tmp_path / "executors.yaml")
    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert fragment in caught.value.message


def test_cache_reloads_only_when_mtime_changes(tmp_path: Path) -> None:
    path = _write_executors(tmp_path, "one: {harness: pi}\n")
    first = load_executor_profiles(tmp_path)
    assert load_executor_profiles(tmp_path) is first
    path.write_text("two: {harness: codex}\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert set(load_executor_profiles(tmp_path)) == {"two"}


def test_unknown_executor_lists_known_names(tmp_path: Path) -> None:
    _write_executors(tmp_path, "beta: {}\nalpha: {}\n")
    with pytest.raises(OmnigentError) as caught:
        get_executor_profile("nope", tmp_path)
    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert "alpha, beta" in caught.value.message


def test_apply_fills_only_unset_fields() -> None:
    profile = ExecutorProfile(
        name="deep", agent_id="ag_1", harness="pi", model="opus", reasoning_effort="high"
    )
    body = ProjectSessionCreateRequest.model_validate({"model_override": "sonnet"})
    filled = apply_executor_profile(body, profile)
    assert (filled.agent_id, filled.harness_override) == ("ag_1", "pi")
    assert (filled.model_override, filled.reasoning_effort) == ("sonnet", "high")
    assert body.harness_override is None  # the input is not mutated


def test_apply_fills_host_fields_unless_host_id_is_explicit() -> None:
    profile = ExecutorProfile(name="p", host_needs=("gpu",), host_preference="hpz4")
    body = ProjectSessionCreateRequest.model_validate({"agent_id": "ag_1", "workspace": "/w"})
    applied = apply_executor_profile(body, profile)
    assert (applied.host_needs, applied.host_preference) == (["gpu"], "hpz4")
    pinned = ProjectSessionCreateRequest.model_validate(
        {"agent_id": "ag_1", "workspace": "/w", "host_id": "a" * 32}
    )
    assert apply_executor_profile(pinned, profile) is pinned


def test_role_declares_executor_and_workspace_wins_only_when_trusted(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write_role(home, "Bench.md", "---\nexecutor: home-exec\n---\nHome.\n")
    _write_role(workspace, "Bench.md", "---\nexecutor: ws-exec\n---\nWorkspace.\n")
    assert role_executor_name("Bench", str(workspace), home=home) == "home-exec"
    trust = home / ".omnigent" / "trusted_role_workspaces"
    trust.write_text(f"{workspace}\n")
    assert role_executor_name("Bench", str(workspace), home=home) == "ws-exec"


def test_role_without_executor_or_alias_yields_none(tmp_path: Path) -> None:
    _write_role(tmp_path, "Bench.md", "---\ndescription: x\n---\nText\n")
    assert role_executor_name("Bench", None, home=tmp_path) is None
    assert role_executor_name("Missing", None, home=tmp_path) is None
    assert role_executor_name(None, None, home=tmp_path) is None

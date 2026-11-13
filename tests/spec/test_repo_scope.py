from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from omnigent.spec.repo_scope import (
    is_trusted_dir,
    repo_scope_dirs,
    skill_scope_dirs,
    untrusted_reason,
)


@pytest.mark.parametrize("outside_repo", ["self", "ancestors"])
def test_walk_ends_at_the_repository_root(
    tmp_path: Path, outside_repo: Literal["self", "ancestors"]
) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "pkg" / "app"
    workspace.mkdir(parents=True)
    # A worktree's .git file marks the root like a .git directory.
    (repo / ".git").write_text("gitdir: elsewhere\n")
    link = tmp_path / "link"
    link.symlink_to(workspace)

    assert repo_scope_dirs(link, outside_repo=outside_repo) == [
        workspace.resolve(),
        (repo / "pkg").resolve(),
        repo.resolve(),
    ]


def test_outside_repo_selects_the_workspace_or_its_ancestors(tmp_path: Path) -> None:
    workspace = tmp_path / "parent" / "app"
    workspace.mkdir(parents=True)

    # Stopping at tmp_path keeps any checkout around it out of the walk.
    assert repo_scope_dirs(workspace, home=tmp_path, outside_repo="self") == [workspace.resolve()]
    assert repo_scope_dirs(workspace, home=tmp_path, outside_repo="ancestors") == [
        workspace.resolve(),
        (tmp_path / "parent").resolve(),
    ]


@pytest.mark.parametrize("outside_repo", ["self", "ancestors"])
def test_home_ends_the_walk_before_an_enclosing_repository(
    tmp_path: Path, outside_repo: Literal["self", "ancestors"]
) -> None:
    (tmp_path / ".git").mkdir()
    home = tmp_path / "home"
    workspace = home / "project"
    workspace.mkdir(parents=True)

    assert repo_scope_dirs(workspace, home=home, outside_repo=outside_repo) == [
        workspace.resolve()
    ]
    assert repo_scope_dirs(home, home=home, outside_repo=outside_repo) == []


@pytest.fixture(autouse=True)
def _private_umask() -> Iterator[None]:
    """Create test directories without group write, whatever the host umask is."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_skill_scope_ends_at_the_repository_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "app"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    assert skill_scope_dirs(workspace, home=tmp_path / "home") == [
        workspace.resolve(),
        repo.resolve(),
    ]


def test_skill_scope_outside_a_repository_ends_short_of_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = home / "scratch" / "app"
    workspace.mkdir(parents=True)
    assert skill_scope_dirs(workspace, home=home) == [
        workspace.resolve(),
        workspace.parent.resolve(),
    ]


def test_skill_scope_outside_home_and_any_repository_keeps_only_the_workspace(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    workspace = shared / "app"
    workspace.mkdir(parents=True)
    shared.chmod(0o1777)
    assert skill_scope_dirs(workspace, home=tmp_path / "home") == [workspace.resolve()]


def _fake_groups(monkeypatch: pytest.MonkeyPatch, *, private: bool) -> None:
    """Make the owner's group a user-private group, or one shared with another user."""
    import grp
    import pwd

    gid = os.getgid()
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="owner", pw_gid=gid))
    monkeypatch.setattr(
        grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name="owner", gr_mem=[] if private else ["someone-else"]),
    )


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
@pytest.mark.parametrize(
    "mode,private_group,trusted",
    [
        (0o755, False, True),
        (0o775, True, True),
        (0o775, False, False),
        (0o757, True, False),
        (0o1777, True, False),
    ],
    ids=["private", "user-private-group", "shared-group", "other-write", "sticky-shared"],
)
def test_untrusted_reason_by_write_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    private_group: bool,
    trusted: bool,
) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    os.chown(skills, -1, os.getgid())
    skills.chmod(mode)
    _fake_groups(monkeypatch, private=private_group)
    assert (untrusted_reason(skills) is None) is trusted


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_untrusted_reason_rejects_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    if os.getuid() == 0:
        pytest.skip("root owns everything it creates")
    monkeypatch.setattr(os, "getuid", lambda: os.getgid() + 100_000)
    reason = untrusted_reason(skills)
    assert reason is not None and "owned by another user" in reason


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_untrusted_reason_checks_a_linked_skill_target_and_its_parent(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    own = tmp_path / "library" / "skills" / "own"
    own.mkdir(parents=True)
    shared = tmp_path / "shared"
    (shared / "planted").mkdir(parents=True)
    shared.chmod(0o777)
    (skills / "own").symlink_to(own, target_is_directory=True)
    (skills / "planted").symlink_to(shared / "planted", target_is_directory=True)

    assert untrusted_reason(skills / "own") is None
    reason = untrusted_reason(skills / "planted")
    assert reason is not None and str(shared) in reason


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_is_trusted_dir_warns_once_per_dir_naming_only_the_dir_and_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    skills = tmp_path / ".claude" / "skills"
    (skills / "planted").mkdir(parents=True)
    (skills / "planted" / "SKILL.md").write_text("---\nname: planted\n---\nsecret body\n")
    skills.chmod(0o777)
    with caplog.at_level(logging.WARNING, logger="omnigent.spec.repo_scope"):
        assert not is_trusted_dir(skills)
        assert not is_trusted_dir(skills)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert str(skills) in message and "writable by other users" in message
    assert "secret body" not in message and "planted" not in message


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_skill_scope_ignores_a_repository_marker_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    workspace = shared / "app"
    workspace.mkdir(parents=True)
    (shared / ".git").mkdir()
    home = tmp_path / "home"
    assert skill_scope_dirs(workspace, home=home) == [workspace.resolve(), shared.resolve()]
    if os.getuid() == 0:
        pytest.skip("root owns everything it creates")
    monkeypatch.setattr(os, "getuid", lambda: os.getgid() + 100_000)
    assert skill_scope_dirs(workspace, home=home) == [workspace.resolve()]


def test_untrusted_reason_fails_closed_when_a_path_cannot_be_checked(tmp_path: Path) -> None:
    reason = untrusted_reason(tmp_path / "missing" / "skills")
    assert reason is not None and "cannot be checked" in reason


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
@pytest.mark.parametrize("mode,trusted", [(0o1777, True), (0o777, False)], ids=["sticky", "open"])
def test_untrusted_reason_checks_world_writable_ancestors(
    tmp_path: Path, mode: int, trusted: bool
) -> None:
    shared = tmp_path / "shared"
    skills = shared / "project" / ".claude" / "skills"
    skills.mkdir(parents=True)
    shared.chmod(mode)
    assert (untrusted_reason(skills) is None) is trusted

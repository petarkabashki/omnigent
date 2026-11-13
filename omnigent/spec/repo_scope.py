"""Walk a workspace up to its repository root, as vendor CLIs do for project config."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)
_warned_untrusted: set[tuple[str, str]] = set()


def repo_scope_dirs(
    workspace: Path,
    *,
    home: Path | None = None,
    outside_repo: Literal["self", "ancestors"],
) -> list[Path]:
    """
    Return *workspace* and its ancestors up to the repository root, nearest first.

    The walk ends at the first directory holding ``.git`` (a worktree's ``.git``
    file counts), and stops short of *home* when one is given. Outside a
    repository, ``"self"`` keeps only the workspace and ``"ancestors"`` keeps
    every directory walked.

    :param workspace: The CLI's working directory, e.g. ``/repo/app``.
    :param home: The user home directory, which is never returned and ends the
        walk, or ``None`` to walk past it.
    :param outside_repo: What to return when no ``.git`` is found.
    :returns: Real paths of the directories in scope, nearest first.
    """
    # realpath, unlike Path.resolve, tolerates symlink loops.
    current = Path(os.path.realpath(workspace))
    stop = Path(os.path.realpath(home)) if home is not None else None
    walked: list[Path] = []
    for directory in (current, *current.parents):
        if directory == stop:
            break
        walked.append(directory)
        if (directory / ".git").exists():
            return walked
    return walked if outside_repo == "ancestors" else walked[:1]


def skill_scope_dirs(workspace: Path, *, home: Path) -> list[Path]:
    """
    Return the workspace directories whose skill folders a session may read.

    Inside a repository the walk ends at its root; outside one it ends short of
    *home*. A workspace outside both keeps only itself, so skill discovery never
    climbs into shared directories such as ``/tmp``.

    :param workspace: The session's working directory.
    :param home: The real user home, which is the user tier and never returned.
    :returns: Real paths, nearest first.
    """
    walked = repo_scope_dirs(workspace, home=home, outside_repo="ancestors")
    if not walked:
        return []
    top = walked[-1]
    if (top / ".git").exists() or top.parent == Path(os.path.realpath(home)):
        return walked
    return walked[:1]


def _is_private_group(gid: int, owner_uid: int) -> bool:
    """Whether *gid* is the owner's user-private group with no other members."""
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(owner_uid)
        group = grp.getgrgid(gid)
    except (ImportError, KeyError):
        return False
    return gid == owner.pw_gid and group.gr_name == owner.pw_name and not group.gr_mem


def untrusted_reason(path: Path) -> str | None:
    """
    Return why *path* lies outside the trust boundary, or ``None`` if it is trusted.

    The resolved target and its parent must be owned by this user or root, and
    writable by nobody else: group write is allowed only for the owner's
    user-private group. Missing paths are trusted; callers skip them anyway.
    """
    if not hasattr(os, "getuid"):
        return None
    target = Path(os.path.realpath(path))
    for candidate in (target, target.parent):
        try:
            info = candidate.stat()
        except OSError:
            return None
        if info.st_uid not in (os.getuid(), 0):
            return f"{candidate} is owned by another user"
        if info.st_mode & stat.S_IWOTH:
            return f"{candidate} is writable by other users"
        if info.st_mode & stat.S_IWGRP and not _is_private_group(info.st_gid, info.st_uid):
            return f"{candidate} is writable by a shared group"
    return None


def is_trusted_dir(path: Path) -> bool:
    """Check :func:`untrusted_reason`, warning once per directory and reason."""
    reason = untrusted_reason(path)
    if reason is None:
        return True
    key = (str(path), reason)
    if key not in _warned_untrusted:
        _warned_untrusted.add(key)
        _log.warning("Skipping skills under %s: %s", path, reason)
    return False

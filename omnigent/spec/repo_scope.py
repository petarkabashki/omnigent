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
    if _owned_repo_marker(top / ".git") or top.parent == Path(os.path.realpath(home)):
        return walked
    return walked[:1]


def _owned_repo_marker(marker: Path) -> bool:
    """Whether *marker* exists and, on POSIX, belongs to this user or root."""
    try:
        owner = marker.lstat().st_uid
    except OSError:
        return False
    return not hasattr(os, "getuid") or owner in (os.getuid(), 0)


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
    user-private group. No ancestor may be world-writable without the sticky
    bit. A path that cannot be checked is untrusted.
    """
    if not hasattr(os, "getuid"):
        return None
    target = Path(os.path.realpath(path))
    for candidate in (target, target.parent):
        try:
            info = candidate.stat()
        except OSError as exc:
            return f"{candidate} cannot be checked ({exc.strerror})"
        if info.st_uid not in (os.getuid(), 0):
            return f"{candidate} is owned by another user"
        if info.st_mode & stat.S_IWOTH:
            return f"{candidate} is writable by other users"
        if info.st_mode & stat.S_IWGRP and not _is_private_group(info.st_gid, info.st_uid):
            return f"{candidate} is writable by a shared group"
    # Anyone can rename entries of a world-writable, non-sticky ancestor.
    for ancestor in target.parent.parents:
        try:
            mode = ancestor.stat().st_mode
        except OSError as exc:
            return f"{ancestor} cannot be checked ({exc.strerror})"
        if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
            return f"{ancestor} is writable by other users"
    return None


def is_trusted_dir(path: Path, *, warnings: list[str] | None = None) -> bool:
    """Check :func:`untrusted_reason`, logging once per directory and reason.

    :param warnings: When given, a user-facing note is appended for each skip, so
        an API caller can show the catalog as filtered.
    """
    reason = untrusted_reason(path)
    if reason is None:
        return True
    if warnings is not None:
        note = f"Skipped skills under {path}: {reason}"
        if note not in warnings:
            warnings.append(note)
    key = (str(path), reason)
    if key not in _warned_untrusted:
        _warned_untrusted.add(key)
        _log.warning("Skipping skills under %s: %s", path, reason)
    return False


CLAUDE_AUTOMOUNT_ROOTS_ENV = "CLAUDE_CODE_AUTOMOUNT_ROOTS"


def claude_untrusted_ancestors(workspace: Path, *, home: Path) -> list[Path]:
    """
    Return folders above *workspace* whose project files Claude Code must skip.

    Claude Code reads ``.claude/skills``, ``CLAUDE.md`` and ``.mcp.json`` from
    folders above its cwd. These are the ones :func:`skill_scope_dirs` leaves out
    or whose skills folder is untrusted. Shared folders outside *home* are listed
    even when empty, since a skill can be planted there after launch; *home*
    itself, the user tier, never is.

    :param workspace: The session's working directory.
    :param home: The real user home.
    :returns: Real paths, nearest first.
    """
    real_workspace = Path(os.path.realpath(workspace))
    real_home = Path(os.path.realpath(home))
    scope = set(skill_scope_dirs(real_workspace, home=real_home))
    skipped: list[Path] = []
    for folder in (real_workspace, *real_workspace.parents):
        if folder == real_home:
            continue
        skills = folder / ".claude" / "skills"
        if folder in scope:
            if skills.exists() and not is_trusted_dir(skills):
                skipped.append(folder)
        elif not folder.is_relative_to(real_home) or skills.exists():
            skipped.append(folder)
    return skipped


def with_claude_automount_roots(existing: str | None, folders: list[Path]) -> str | None:
    """Merge *folders* into a ``CLAUDE_CODE_AUTOMOUNT_ROOTS`` value, keeping *existing* first.

    A path holding the ``:`` separator cannot be listed and is left out.
    """
    entries = [entry for entry in (existing or "").split(":") if entry]
    for folder in folders:
        text = str(folder)
        if ":" not in text and text not in entries:
            entries.append(text)
    return ":".join(entries) or None

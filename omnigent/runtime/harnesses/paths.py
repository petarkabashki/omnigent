"""Filesystem paths shared by host and harness-process lifecycle code."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnigent._platform import IS_WINDOWS

# Relocates the per-user runtime parent. A parent outside /tmp escapes tmpfiles
# aging, so files left there (bang logs, srt settings) persist until removed.
HARNESS_TMP_PARENT_ENV_VAR = "OMNIGENT_HARNESS_TMP_PARENT"


def harness_tmp_parent(env: Mapping[str, str] | None = None) -> Path:
    """Return the configured harness socket root without changing relativity."""
    source = os.environ if env is None else env
    configured = source.get(HARNESS_TMP_PARENT_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    if IS_WINDOWS:
        return Path(tempfile.gettempdir()) / "omnigent"
    return Path(f"/tmp/omnigent-{os.getuid()}")


def absolute_harness_tmp_parent(path: Path) -> Path:
    """Make a harness socket root absolute without resolving symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def resolve_harness_tmp_parent(env: Mapping[str, str] | None = None) -> Path:
    """Return one short absolute socket root for cross-process propagation."""
    return absolute_harness_tmp_parent(harness_tmp_parent(env))


def harness_bridge_root(name: str, env: Mapping[str, str] | None = None) -> Path:
    """
    Return a native bridge's root under the runtime parent, without creating it.

    :param name: Bridge root name, e.g. ``"kimi-native"``.
    :returns: e.g. ``Path("/tmp/omnigent-1000/kimi-native")``.
    """
    return resolve_harness_tmp_parent(env) / name


# AF_UNIX ``sun_path`` holds 108 bytes on Linux and 104 on macOS, NUL included.
UNIX_SOCKET_PATH_MAX = 103
# Room for the deepest sockets under the parent: "/model-signer-private-xxxxxxxx/relay.sock"
# (41 bytes) and the harness "/ap-<12 hex>/c-<16 hex>.sock" (40 bytes).
RUNTIME_SOCKET_HEADROOM = 48
# Longest runtime parent, in bytes, that still fits every socket under it.
RUNTIME_PARENT_MAX = UNIX_SOCKET_PATH_MAX - RUNTIME_SOCKET_HEADROOM


def ensure_private_dir(path: Path, my_uid: int | None) -> None:
    """
    Create ``path`` as a 0o700 directory, or validate an existing one.

    :param path: Directory that must be owner-only, e.g. one bridge ancestor
        or the harness socket root.
    :param my_uid: Current uid, or ``None`` where POSIX ownership does not
        apply (Windows), which skips the owner and mode checks.
    :raises RuntimeError: If ``path`` exists as a symlink, a non-directory,
        or a directory owned by another uid.
    """
    try:
        os.mkdir(path, mode=0o700)
        return
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing to use {path!s}: is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"refusing to use {path!s}: not a directory")
    if my_uid is not None and st.st_uid != my_uid:
        raise RuntimeError(
            f"refusing to use {path!s}: owned by uid {st.st_uid}, not current user ({my_uid})"
        )
    if my_uid is not None and (st.st_mode & 0o077) != 0:
        os.chmod(path, 0o700)


def ensure_runtime_parent(env: Mapping[str, str] | None = None) -> Path:
    """
    Create or validate the per-user parent of every Omnigent runtime dir.

    :param env: Environment to read the override from; defaults to ``os.environ``.
    :returns: The absolute parent, e.g. ``Path("/tmp/omnigent-1000")``.
    :raises RuntimeError: If the parent is too long to hold Unix sockets, or
        is a symlink, a non-directory, or owned by another uid.
    """
    parent = resolve_harness_tmp_parent(env)
    length = len(os.fsencode(parent))
    if not IS_WINDOWS and length > RUNTIME_PARENT_MAX:
        raise RuntimeError(
            f"runtime parent {parent} is {length} bytes; the limit is {RUNTIME_PARENT_MAX} "
            f"bytes, so Unix sockets under it fit. Shorten {HARNESS_TMP_PARENT_ENV_VAR}"
        )
    parent.parent.mkdir(parents=True, exist_ok=True)
    getuid = getattr(os, "getuid", None)
    ensure_private_dir(parent, getuid() if getuid is not None else None)
    return parent


def runtime_mkdtemp(prefix: str, env: Mapping[str, str] | None = None) -> Path:
    """
    Make a private temp dir under :func:`ensure_runtime_parent`.

    :param prefix: Name prefix, e.g. ``"terminal-"``.
    :returns: The new 0o700 directory, e.g. ``/tmp/omnigent-1000/terminal-ab12cd``.
    """
    return Path(tempfile.mkdtemp(prefix=prefix, dir=ensure_runtime_parent(env)))

"""Read role text and skills from a shared agent library through its resolve command.

A workspace opts in with ``.agents/library.yaml``; the hub names the command in
``~/.omnigent/config.yaml``::

    agent_library:
      command: agent-library        # or a list, e.g. [uv, run, agent-library]

The command runs as ``<command> resolve --project <root> --json [--role <name>]``
and prints the ``agent-library.resolve`` format, version 1. Any failure falls back
to today's role files and skill directories, so the library never blocks a session.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shlex
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _parse_skill
from omnigent.spec.types import SkillSpec

_logger = logging.getLogger(__name__)

CONFIG_KEY = "agent_library"
PROJECT_FILE_PARTS = (".agents", "library.yaml")
RESOLVE_FORMAT = "agent-library.resolve"
RESOLVE_FORMAT_VERSION = 1
RESOLVE_TIMEOUT_SECONDS = 10.0
# Skill directories a project fills itself; a copy there counts as the project layer.
PROJECT_SKILL_DIRS = ((".claude", "skills"), (".agents", "skills"))

# custom-lint: disable-next=workspace-scoped-cache -- log dedupe keyed by a fallback cause
_warned: set[str] = set()
# custom-lint: disable-next=workspace-scoped-cache -- keyed by host command and project path
_cache: dict[tuple[tuple[str, ...], str, str | None], tuple[str, LibraryResolution]] = {}
_cache_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class LibraryRole:
    """A resolved role: ``body`` is final, with ``extends`` already applied."""

    name: str
    path: Path
    body: str
    metadata: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class LibrarySkill:
    """A resolved skill; ``layer`` is ``core``, ``collection:<name>`` or ``project``."""

    name: str
    layer: str
    path: Path
    file: Path


@dataclasses.dataclass(frozen=True)
class LibraryResolution:
    """What the library gives one project scope."""

    library_root: Path | None
    project_root: Path
    roles: tuple[LibraryRole, ...]
    skills: tuple[LibrarySkill, ...]

    def role(self, name: str) -> LibraryRole | None:
        """Return the role named ``name``, matched case-insensitively like role files."""
        wanted = name.casefold()
        return next((role for role in self.roles if role.name.casefold() == wanted), None)


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log a fallback warning once per cause, so a broken setup doesn't flood the log."""
    if key in _warned:
        return
    _warned.add(key)
    _logger.warning(message, *args)


def configured_command(config: Mapping[str, object] | None = None) -> tuple[str, ...] | None:
    """Return the resolve command from the user config, or ``None`` when unset."""
    if config is None:
        from omnigent.config import load_global_config

        try:
            config = load_global_config()
        except (OSError, yaml.YAMLError) as exc:
            _warn_once("config", "Agent library disabled: unreadable user config: %s", exc)
            return None
    section = config.get(CONFIG_KEY)
    raw = section.get("command") if isinstance(section, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        return tuple(shlex.split(raw))
    if isinstance(raw, list) and raw and all(isinstance(part, str) for part in raw):
        return tuple(raw)
    if raw is not None:
        _warn_once(
            "command", "Agent library disabled: %s.command must be a string or list", CONFIG_KEY
        )
    return None


def find_project_root(workspace: str | Path) -> Path | None:
    """Return the nearest directory at or above ``workspace`` with ``.agents/library.yaml``.

    The search stops at the git root, where the library itself defaults the project.
    """
    current = Path(workspace).expanduser().resolve()
    while True:
        if current.joinpath(*PROJECT_FILE_PARTS).is_file():
            return current
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


def _fingerprint(*roots: Path | None) -> str:
    """Digest of every path, mtime and size under the roots; any library edit misses the cache."""
    digest = hashlib.sha256()
    for root in roots:
        if root is None:
            continue
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = sorted(name for name in subdirs if name != ".git")
            for name in (".", *sorted(files)):
                path = os.path.join(directory, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                digest.update(f"{path}\0{info.st_mtime_ns}\0{info.st_size}\n".encode())
    return digest.hexdigest()


def _run_resolve(
    command: Sequence[str], project_root: Path, role: str | None
) -> tuple[int, Mapping[str, object]] | None:
    """Run the resolve command; ``None`` (after one warning) when it gives no usable output."""
    argv = [*command, "resolve", "--project", str(project_root), "--json"]
    if role:
        argv += ["--role", role]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=RESOLVE_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        _warn_once("missing", "Agent library disabled: command not found: %s", command[0])
        return None
    except subprocess.TimeoutExpired:
        _warn_once(
            "timeout",
            "Agent library resolve timed out after %ss; falling back to role files",
            RESOLVE_TIMEOUT_SECONDS,
        )
        return None
    except OSError as exc:
        _warn_once("oserror", "Agent library resolve could not start: %s", exc)
        return None
    if completed.returncode not in (0, 1):
        detail = completed.stderr.strip().splitlines()[-1:] or ["no detail"]
        _warn_once(
            f"exit{completed.returncode}",
            "Agent library resolve exited %s for %s; falling back to role files: %s",
            completed.returncode,
            project_root,
            detail[0],
        )
        return None
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        _warn_once("json", "Agent library resolve printed no JSON; falling back to role files")
        return None
    if not isinstance(document, dict) or document.get("format") != RESOLVE_FORMAT:
        _warn_once(
            "format",
            "Agent library resolve printed an unknown format; falling back to role files",
        )
        return None
    version = document.get("format_version")
    if version != RESOLVE_FORMAT_VERSION:
        _warn_once(
            f"version{version}",
            "Agent library resolve format_version %r is not %s; falling back to role files",
            version,
            RESOLVE_FORMAT_VERSION,
        )
        return None
    return completed.returncode, document


def _entries(document: Mapping[str, object], kind: str) -> list[Mapping[str, object]]:
    assets = document.get("assets")
    items = assets.get(kind) if isinstance(assets, Mapping) else None
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def parse_resolution(document: Mapping[str, object]) -> LibraryResolution | None:
    """Parse a resolve document; ``None`` when ``scope.project_file`` is null (not opted in).

    Unknown keys are ignored; an entry missing a required field is skipped.
    """
    scope = document.get("scope")
    if not isinstance(scope, Mapping) or not scope.get("project_file") or not scope.get("root"):
        return None
    library = document.get("library")
    library_root = library.get("root") if isinstance(library, Mapping) else None
    roles: list[LibraryRole] = []
    for item in _entries(document, "roles"):
        name, path, body = item.get("name"), item.get("path"), item.get("body")
        if isinstance(name, str) and isinstance(path, str) and isinstance(body, str):
            metadata = item.get("metadata")
            roles.append(
                LibraryRole(
                    name, Path(path), body, metadata if isinstance(metadata, Mapping) else {}
                )
            )
    skills: list[LibrarySkill] = []
    for item in _entries(document, "skills"):
        name, layer, path, file = (item.get(key) for key in ("name", "layer", "path", "file"))
        if all(isinstance(value, str) for value in (name, layer, path, file)):
            skills.append(LibrarySkill(str(name), str(layer), Path(str(path)), Path(str(file))))
    return LibraryResolution(
        library_root=Path(library_root) if isinstance(library_root, str) else None,
        project_root=Path(str(scope["root"])),
        roles=tuple(roles),
        skills=tuple(skills),
    )


def _log_diagnostics(document: Mapping[str, object], project_root: Path) -> None:
    """Log each warning and error the resolve reported, e.g. a left-out skill or unknown role."""
    diagnostics = document.get("diagnostics")
    for item in diagnostics if isinstance(diagnostics, list) else []:
        if isinstance(item, Mapping) and item.get("level") in ("warning", "error"):
            _logger.warning(
                "Agent library %s %s %s/%s for %s: %s",
                item.get("level"),
                item.get("code", "?"),
                item.get("type", "?"),
                item.get("name", "?"),
                project_root,
                item.get("message", ""),
            )


def resolve_library(
    workspace: str | Path | None,
    role: str | None = None,
    *,
    command: Sequence[str] | None = None,
) -> LibraryResolution | None:
    """Resolve the library for a session's workspace, or ``None`` to keep today's behaviour.

    ``None`` when no command is configured, the workspace has no project file, or
    the command fails. Results are cached until a file under the library or the
    project's ``.agents`` changes.

    :param workspace: The session's workspace directory.
    :param role: The role the session holds, passed as ``--role``.
    :param command: The resolve command; defaults to the user config's setting.
    """
    if not workspace:
        return None
    project_root = find_project_root(workspace)
    if project_root is None:
        return None
    argv = tuple(command) if command is not None else configured_command()
    if not argv:
        return None
    key = (argv, str(project_root), role.casefold() if role else None)
    agents_dir = project_root / PROJECT_FILE_PARTS[0]
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        fingerprint, resolution = cached
        if fingerprint == _fingerprint(resolution.library_root, agents_dir):
            return resolution
    result = _run_resolve(argv, project_root, role)
    if result is None:
        return None
    # Only a fresh resolve logs, so each diagnostic appears once per cache entry.
    _log_diagnostics(result[1], project_root)
    resolution = parse_resolution(result[1])
    if resolution is None:
        return None
    with _cache_lock:
        _cache[key] = (_fingerprint(resolution.library_root, agents_dir), resolution)
    return resolution


def _project_skill_copy(project_root: Path, workspace: Path, name: str) -> Path | None:
    """Return the project's own ``.claude/skills`` or ``.agents/skills`` copy of ``name``."""
    for root in dict.fromkeys((workspace, project_root)):
        for parts in PROJECT_SKILL_DIRS:
            candidate = root.joinpath(*parts, name)
            if (candidate / "SKILL.md").is_file():
                return candidate
    return None


def library_skills(
    resolution: LibraryResolution,
    workspace: str | Path,
    skills_filter: str | list[str] = "all",
) -> list[SkillSpec]:
    """Return the library's skills as specs, one per name.

    A project's own copy outranks a core or collection skill, as the project
    layer does, so that skill is left to the project's directory. A copy that is a
    link to the library's winner is the same skill and keeps the library entry.
    """
    if skills_filter == "none":
        return []
    wanted = set(skills_filter) if isinstance(skills_filter, list) else None
    workspace_dir = Path(workspace).expanduser().resolve()
    out: list[SkillSpec] = []
    for skill in resolution.skills:
        if wanted is not None and skill.name not in wanted:
            continue
        if skill.layer != "project":
            copy = _project_skill_copy(resolution.project_root, workspace_dir, skill.name)
            if copy is not None and copy.resolve() != skill.path.resolve():
                continue
        try:
            spec = _parse_skill(skill.file)
        except (OmnigentError, OSError) as exc:
            _logger.warning("Skipping agent library skill %s: %s", skill.file, exc)
            continue
        out.append(dataclasses.replace(spec, name=skill.name))
    return out


def clear_cache() -> None:
    """Forget cached resolutions and warnings; for tests."""
    with _cache_lock:
        _cache.clear()
    _warned.clear()

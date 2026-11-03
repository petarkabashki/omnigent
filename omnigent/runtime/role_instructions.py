"""Per-role overlays for sessions addressed by a role alias.

A session aliased ``Architect`` loads ``~/.omnigent/roles/Architect.md`` (or
``Architect/ROLE.md``) as the base and layers
``<workspace>/.omnigent/roles/Architect.md`` on top. A role file is Markdown
with optional YAML front matter::

    ---
    description: Owns the design
    params: {repo: omnigent}
    skills: [skills/review, find-skills]   # folder path or a named skill
    mcp_servers:
      notes: {command: notes-mcp, args: ["--root", "${workspace}"]}
    ---
    Review ${params.repo} designs.

The overlay is additive (see :func:`apply_role_overlay`): it can add text,
params, skills and MCP servers, never change the executor, sandbox or policies.
Workspace roles come from a cloned repo, so their skills and MCP servers load
only when the workspace is listed in ``~/.omnigent/trusted_role_workspaces``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml

from omnigent.errors import OmnigentError
from omnigent.session_alias_client import ALIAS_PATTERN
from omnigent.spec.parser import (
    _FRONTMATTER_RE,
    _parse_inline_mcp_servers,
    _parse_skill,
    discover_host_skills,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig, SkillSpec

_logger = logging.getLogger(__name__)

ROLES_DIR_PARTS = (".omnigent", "roles")
ROLE_FOLDER_FILE = "ROLE.md"
TRUSTED_WORKSPACES_PARTS = (".omnigent", "trusted_role_workspaces")
ROLE_PARAM_LABEL_PREFIX = "omnigent.role_param."
MAX_ROLE_FILE_BYTES = 64 * 1024

# ``${params.x}`` and lowercase names are role placeholders; uppercase
# ``${VAR}`` stays an environment reference for the spec parser.
_PLACEHOLDER_RE = re.compile(r"\$\{(params\.[A-Za-z0-9_-]+|[a-z][a-z0-9_]*)\}")

_Named = TypeVar("_Named", SkillSpec, MCPServerConfig)


@dataclasses.dataclass(frozen=True)
class RoleSource:
    """One role definition found on disk.

    :param path: The Markdown file, e.g. ``~/.omnigent/roles/Bench/ROLE.md``.
    :param trusted: Whether this source may add skills and MCP servers.
    """

    path: Path
    trusted: bool


@dataclasses.dataclass(frozen=True)
class RoleOverlay:
    """The merged role for one session, ready for :func:`apply_role_overlay`."""

    alias: str
    instructions: str | None
    params: dict[str, str]
    skills: list[SkillSpec]
    mcp_servers: list[MCPServerConfig]


def _casefold_child(directory: Path, name: str) -> Path | None:
    """Return ``directory/name``, matching the name case-insensitively."""
    exact = directory / name
    if exact.exists():
        return exact
    if not directory.is_dir():
        return None
    wanted = name.casefold()
    for entry in sorted(directory.iterdir()):
        if entry.name.casefold() == wanted:
            return entry
    return None


def _find_role_file(directory: Path, alias: str) -> Path | None:
    """Return ``<alias>/ROLE.md`` or ``<alias>.md`` under ``directory``."""
    try:
        folder = _casefold_child(directory, alias)
        if folder is not None and folder.is_dir():
            role_md = _casefold_child(folder, ROLE_FOLDER_FILE)
            if role_md is not None and role_md.is_file():
                return role_md
        path = _casefold_child(directory, f"{alias}.md")
    except OSError as exc:
        _logger.warning("Could not read role instructions in %s: %s", directory, exc)
        return None
    return path if path is not None and path.is_file() else None


def is_trusted_role_workspace(workspace: str, home: Path | None = None) -> bool:
    """Whether ``workspace`` is listed in ``~/.omnigent/trusted_role_workspaces``."""
    listing = (home or Path.home()).joinpath(*TRUSTED_WORKSPACES_PARTS)
    try:
        lines = listing.read_text().splitlines()
    except OSError:
        return False
    target = Path(workspace).expanduser().resolve()
    entries = (Path(line.strip()) for line in lines if line.strip())
    return any(entry.is_absolute() and entry.resolve() == target for entry in entries)


def find_role_sources(
    alias: str, workspace: str | None, *, home: Path | None = None
) -> list[RoleSource]:
    """Return the home role then the workspace role, whichever exist."""
    home_file = _find_role_file((home or Path.home()).joinpath(*ROLES_DIR_PARTS), alias)
    sources = [RoleSource(home_file, trusted=True)] if home_file is not None else []
    if workspace:
        ws_dir = Path(workspace).expanduser().joinpath(*ROLES_DIR_PARTS)
        ws_file = _find_role_file(ws_dir, alias)
        if ws_file is not None and (home_file is None or ws_file.resolve() != home_file.resolve()):
            trusted = is_trusted_role_workspace(workspace, home)
            sources.append(RoleSource(ws_file, trusted=trusted))
    return sources


def split_role_file(text: str) -> tuple[dict[str, object], str]:
    """Split optional YAML front matter from a role file's Markdown body.

    Unreadable front matter is dropped with a warning; the body still loads.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        _logger.warning("Ignoring unreadable role front matter: %s", exc)
        meta = None
    return (meta if isinstance(meta, dict) else {}), match.group(2)


def role_executor_name(
    alias: str | None, workspace: str | None, *, home: Path | None = None
) -> str | None:
    """Executor profile name a role file declares (``executor:`` front matter), if any.

    The workspace role overrides the home one, but an untrusted workspace role
    cannot set an executor. Reads the calling process's role files, so on the
    server this only reflects roles on the server's own host.
    """
    if not alias or not ALIAS_PATTERN.match(alias):
        return None
    chosen: str | None = None
    for source in find_role_sources(alias, workspace, home=home):
        try:
            raw = source.path.read_bytes()[:MAX_ROLE_FILE_BYTES]
        except OSError:
            continue
        meta, _ = split_role_file(raw.decode("utf-8", errors="replace"))
        name = meta.get("executor")
        if not isinstance(name, str) or not name.strip():
            continue
        if source.trusted:
            chosen = name.strip()
        else:
            _logger.warning("Ignoring executor in untrusted workspace role %s", source.path)
    return chosen


def substitute_placeholders(text: str, values: Mapping[str, str]) -> str:
    """Replace ``${params.x}``, ``${workspace}``, ``${project}``, ``${alias}``.

    Unknown lowercase placeholders are left as-is with a warning; uppercase
    ``${VAR}`` environment references are never touched.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        _logger.warning("Role placeholder ${%s} has no value; left as-is", key)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def _placeholder_values(
    alias: str, workspace: str | None, params: Mapping[str, str]
) -> dict[str, str]:
    values = {f"params.{name}": value for name, value in params.items()}
    values["alias"] = alias
    if workspace:
        values["workspace"] = workspace
        values["project"] = Path(workspace).expanduser().name
    return values


def _string_params(raw: object, source: Path) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _logger.warning("Ignoring non-mapping 'params' in role file %s", source)
        return {}
    return {str(name): str(value) for name, value in raw.items()}


def role_params_from_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    """Session-level role params stored as ``omnigent.role_param.<name>`` labels."""
    prefix = ROLE_PARAM_LABEL_PREFIX
    return {
        key[len(prefix) :]: value
        for key, value in (labels or {}).items()
        if key.startswith(prefix) and len(key) > len(prefix)
    }


def _named_skill_root(source: RoleSource, workspace: str | None, home: Path | None) -> Path:
    """Where to look up a named role skill.

    A home role in an untrusted workspace searches from home only, so a cloned
    repo's ``.claude/skills`` cannot supply a skill under the home role's name.
    """
    home_dir = home or Path.home()
    if workspace and source.trusted and is_trusted_role_workspace(workspace, home):
        return Path(workspace).expanduser()
    return home_dir


def _parse_role_skills(
    raw: object, source: RoleSource, workspace: str | None, home: Path | None
) -> list[SkillSpec]:
    """Parse ``skills:``: a path under the role's folder, or a host skill name."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        _logger.warning("Ignoring non-list 'skills' in role file %s", source.path)
        return []
    skills: list[SkillSpec] = []
    base = source.path.parent.resolve()
    for entry in raw:
        name = str(entry)
        try:
            if "/" in name or name.startswith("."):
                target = (base / name).resolve()
                if not target.is_relative_to(base):
                    raise OmnigentError(f"skill path {name!r} escapes {base}")
                skills.append(_parse_skill(target / "SKILL.md" if target.is_dir() else target))
                continue
            found = discover_host_skills(_named_skill_root(source, workspace, home), [name])
            if not found:
                raise OmnigentError(f"no skill named {name!r}")
            skills.extend(found)
        except (OmnigentError, OSError) as exc:
            _logger.warning("Skipping role skill %r from %s: %s", name, source.path, exc)
    return skills


def _parse_role_mcp_servers(
    raw: object, source: RoleSource, values: Mapping[str, str]
) -> list[MCPServerConfig]:
    """Parse ``mcp_servers:`` with the agent spec's inline MCP parser."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        _logger.warning("Ignoring non-mapping 'mcp_servers' in role file %s", source.path)
        return []
    entries: dict[str, dict[str, object]] = {}
    for name, config in raw.items():
        if not isinstance(config, dict):
            continue
        entry: dict[str, object] = {**config, "type": "mcp"}
        if "command" in entry:
            entry["command"] = substitute_placeholders(str(entry["command"]), values)
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [substitute_placeholders(str(arg), values) for arg in args]
        entries[str(name)] = entry
    try:
        return _parse_inline_mcp_servers(entries)
    except OmnigentError as exc:
        _logger.warning("Ignoring 'mcp_servers' in role file %s: %s", source.path, exc)
        return []


def _merge_by_name(base: list[_Named], overlay: list[_Named]) -> list[_Named]:
    """Append ``overlay`` to ``base``; an overlay entry replaces a same-named one."""
    names = {item.name for item in overlay}
    return [item for item in base if item.name not in names] + list(overlay)


def load_role_overlay(
    alias: str | None,
    workspace: str | None,
    *,
    session_params: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> RoleOverlay | None:
    """Merge the home and workspace role files for ``alias``.

    :param alias: The session's role alias (label ``omnigent.alias``).
    :param workspace: The session's workspace; its role layers over the home one.
    :param session_params: Session-level params; they override file params.
    :param home: Home directory override, for tests.
    :returns: The merged overlay, or ``None`` when no role file exists.
    """
    if not alias or not ALIAS_PATTERN.match(alias):
        return None
    loaded: list[tuple[RoleSource, dict[str, object], str]] = []
    for source in find_role_sources(alias, workspace, home=home):
        try:
            raw = source.path.read_bytes()[:MAX_ROLE_FILE_BYTES]
        except OSError as exc:
            _logger.warning("Could not read role file %s: %s", source.path, exc)
            continue
        meta, body = split_role_file(raw.decode("utf-8", errors="replace"))
        loaded.append((source, meta, body.strip()))
    if not loaded:
        return None

    params: dict[str, str] = {}
    for source, meta, _ in loaded:
        params.update(_string_params(meta.get("params"), source.path))
    params.update(session_params or {})
    values = _placeholder_values(alias, workspace, params)

    description: str | None = None
    sections: list[str] = []
    skills: list[SkillSpec] = []
    mcp_servers: list[MCPServerConfig] = []
    for source, meta, body in loaded:
        raw_description = meta.get("description")
        if isinstance(raw_description, str) and raw_description.strip():
            description = raw_description.strip()
        if body:
            text = substitute_placeholders(body, values)
            sections.append(f"Role instructions from {source.path}:\n\n{text}")
        if not source.trusted:
            if meta.get("skills") or meta.get("mcp_servers"):
                _logger.warning(
                    "Ignoring skills/mcp_servers in untrusted workspace role %s; list the "
                    "workspace in ~/.omnigent/trusted_role_workspaces to allow them",
                    source.path,
                )
            continue
        role_skills = _parse_role_skills(meta.get("skills"), source, workspace, home)
        skills = _merge_by_name(skills, role_skills)
        role_servers = _parse_role_mcp_servers(meta.get("mcp_servers"), source, values)
        mcp_servers = _merge_by_name(mcp_servers, role_servers)

    instructions = None
    if sections:
        summary = f" ({description})" if description else ""
        instructions = f"## Role: {alias}{summary}\n\n" + "\n\n".join(sections)
    return RoleOverlay(alias, instructions, params, skills, mcp_servers)


def load_role_instructions(
    alias: str | None,
    workspace: str | None,
    *,
    session_params: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Return only the role section text for ``alias``; see :func:`load_role_overlay`."""
    overlay = load_role_overlay(alias, workspace, session_params=session_params, home=home)
    return overlay.instructions if overlay is not None else None


def apply_role_overlay(spec: AgentSpec, overlay: RoleOverlay) -> AgentSpec:
    """Return a copy of ``spec`` with the role's text, params, skills and MCP servers.

    Only those four fields change, so a role can never alter the executor,
    harness, model, OS environment, sandbox, guardrails or policies.
    """
    instructions = spec.instructions
    if overlay.instructions and overlay.instructions not in (instructions or ""):
        instructions = "\n\n".join(part for part in (instructions, overlay.instructions) if part)
    params: dict[str, Any] = {**spec.params, **overlay.params}  # type: ignore[explicit-any]
    return dataclasses.replace(
        spec,
        instructions=instructions,
        params=params,
        skills=_merge_by_name(spec.skills, overlay.skills),
        mcp_servers=_merge_by_name(spec.mcp_servers, overlay.mcp_servers),
    )


def apply_session_role(
    spec: AgentSpec, labels: Mapping[str, str] | None, workspace: str | None
) -> AgentSpec:
    """Overlay the role named by the session's ``omnigent.alias`` label, if any."""
    from omnigent.stores.conversation_store import ALIAS_LABEL_KEY

    labels = labels or {}
    overlay = load_role_overlay(
        labels.get(ALIAS_LABEL_KEY), workspace, session_params=role_params_from_labels(labels)
    )
    return apply_role_overlay(spec, overlay) if overlay is not None else spec

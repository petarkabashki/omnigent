"""Per-role overlays for sessions addressed by a role alias.

A session aliased ``Architect`` loads ``~/.omnigent/roles/Architect.md`` (or
``Architect/ROLE.md``) as the base and layers
``<workspace>/.omnigent/roles/Architect.md`` on top. In a workspace that uses the
shared agent library (see :mod:`omnigent.runtime.agent_library`), the library's
role replaces the home file as the base. An ``omnigent.role_overlay``
label names the file instead, so a role can reuse another's file while keeping
its own alias. A role file is Markdown with optional YAML front matter::

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
    :param body: Final text from the agent library; the file is then not read.
    :param meta: Front matter that comes with ``body``.
    """

    path: Path
    trusted: bool
    body: str | None = None
    meta: Mapping[str, object] | None = None


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


# Library metadata keys a role may carry; skills, tools and executors stay out.
_LIBRARY_ROLE_META_KEYS = ("description", "params")


def _library_role_source(alias: str, workspace: str | None) -> RoleSource | None:
    """The agent library's role for ``alias`` when the workspace uses the library."""
    from omnigent.runtime.agent_library import resolve_library

    resolution = resolve_library(workspace, alias)
    role = resolution.role(alias) if resolution is not None else None
    if role is None:
        return None
    meta = {key: role.metadata[key] for key in _LIBRARY_ROLE_META_KEYS if key in role.metadata}
    return RoleSource(role.path, trusted=True, body=role.body, meta=meta)


def find_role_sources(
    alias: str, workspace: str | None, *, home: Path | None = None
) -> list[RoleSource]:
    """Return the base role then the workspace role, whichever exist.

    The base is the agent library's role when the workspace uses the library and
    defines it, else the home role file. A workspace role is used only when the
    workspace is listed in ``~/.omnigent/trusted_role_workspaces``.
    """
    home_file: Path | None = None
    library_source = _library_role_source(alias, workspace)
    if library_source is not None:
        sources = [library_source]
    else:
        home_file = _find_role_file((home or Path.home()).joinpath(*ROLES_DIR_PARTS), alias)
        sources = [RoleSource(home_file, trusted=True)] if home_file is not None else []
    if workspace:
        ws_dir = Path(workspace).expanduser().joinpath(*ROLES_DIR_PARTS)
        ws_file = _find_role_file(ws_dir, alias)
        if ws_file is not None and (home_file is None or ws_file.resolve() != home_file.resolve()):
            if is_trusted_role_workspace(workspace, home):
                sources.append(RoleSource(ws_file, trusted=True))
            else:
                _warn_untrusted_workspace_roles(ws_dir)
    return sources


_warned_untrusted_role_dirs: set[Path] = set()


def _warn_untrusted_workspace_roles(roles_dir: Path) -> None:
    """Log, once per workspace, the role files skipped because it is not trusted."""
    resolved = roles_dir.resolve()
    if resolved in _warned_untrusted_role_dirs:
        return
    _warned_untrusted_role_dirs.add(resolved)
    try:
        names = sorted(
            str(path.relative_to(roles_dir))
            for path in [*roles_dir.glob("*.md"), *roles_dir.glob(f"*/{ROLE_FOLDER_FILE}")]
        )
    except OSError:
        names = []
    _logger.warning(
        "Skipping role files in untrusted workspace %s: %s; list the workspace in "
        "~/.omnigent/trusted_role_workspaces to use them",
        roles_dir,
        ", ".join(names) or "(unreadable)",
    )


def _read_role_source(source: RoleSource) -> tuple[dict[str, object], str] | None:
    """Return a source's front matter and body, or ``None`` when its file is unreadable."""
    if source.body is not None:
        return dict(source.meta or {}), source.body
    try:
        raw = source.path.read_bytes()[:MAX_ROLE_FILE_BYTES]
    except OSError as exc:
        _logger.warning("Could not read role file %s: %s", source.path, exc)
        return None
    return split_role_file(raw.decode("utf-8", errors="replace"))


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

    The workspace role, used only in a trusted workspace, overrides the home one.
    Reads the calling process's role files, so on the server this only reflects
    roles on the server's own host.
    """
    if not alias or not ALIAS_PATTERN.match(alias):
        return None
    chosen: str | None = None
    for source in find_role_sources(alias, workspace, home=home):
        read = _read_role_source(source)
        if read is None:
            continue
        meta, _ = read
        name = meta.get("executor")
        if not isinstance(name, str) or not name.strip():
            continue
        chosen = name.strip()
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
    session_alias: str | None = None,
) -> RoleOverlay | None:
    """Merge the home and workspace role files for ``alias``.

    :param alias: The role-file name (see :func:`load_session_role_overlay`).
    :param workspace: The session's workspace; its role layers over the home one.
    :param session_params: Session-level params; they override file params.
    :param home: Home directory override, for tests.
    :param session_alias: What ``${alias}`` expands to when it is not the file name.
    :returns: The merged overlay, or ``None`` when no role file exists.
    """
    if not alias or not ALIAS_PATTERN.match(alias):
        return None
    loaded: list[tuple[RoleSource, dict[str, object], str]] = []
    for source in find_role_sources(alias, workspace, home=home):
        read = _read_role_source(source)
        if read is None:
            continue
        meta, body = read
        loaded.append((source, meta, body.strip()))
    if not loaded:
        return None

    params: dict[str, str] = {}
    for source, meta, _ in loaded:
        params.update(_string_params(meta.get("params"), source.path))
    params.update(session_params or {})
    values = _placeholder_values(session_alias or alias, workspace, params)

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


def load_session_role_overlay(
    labels: Mapping[str, str] | None,
    workspace: str | None,
    *,
    home: Path | None = None,
) -> RoleOverlay | None:
    """Load the overlay a session's labels name; the one resolver for every loader.

    ``omnigent.role_overlay`` wins over ``omnigent.alias`` when set. The session
    keeps its own alias for addressing and ``${alias}``. Malformed names load
    nothing, so a label can never point outside the roles directories.

    :param labels: The session's labels.
    :param workspace: The session's workspace.
    :param home: Home directory override, for tests.
    :returns: The merged overlay, or ``None`` when no role file exists.
    """
    from omnigent.stores.conversation_store import ALIAS_LABEL_KEY, ROLE_OVERLAY_LABEL_KEY

    labels = labels or {}
    alias = labels.get(ALIAS_LABEL_KEY)
    return load_role_overlay(
        labels.get(ROLE_OVERLAY_LABEL_KEY) or alias,
        workspace,
        session_params=role_params_from_labels(labels),
        home=home,
        session_alias=alias,
    )


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


def session_role_name(labels: Mapping[str, str] | None) -> str | None:
    """The role-file name a session's labels select, or ``None``."""
    from omnigent.stores.conversation_store import ALIAS_LABEL_KEY, ROLE_OVERLAY_LABEL_KEY

    labels = labels or {}
    name = labels.get(ROLE_OVERLAY_LABEL_KEY) or labels.get(ALIAS_LABEL_KEY)
    return name if name and ALIAS_PATTERN.match(name) else None


def apply_library_skills(
    spec: AgentSpec, labels: Mapping[str, str] | None, workspace: str | None
) -> AgentSpec:
    """Add the agent library's skills for the session's workspace and role.

    Skills the spec already names (its bundle or role skills) keep their place.
    """
    from omnigent.runtime.agent_library import library_skills, resolve_library

    if not workspace:
        return spec
    resolution = resolve_library(workspace, session_role_name(labels))
    if resolution is None:
        return spec
    present = {skill.name for skill in spec.skills}
    extra = [
        skill
        for skill in library_skills(resolution, workspace, spec.skills_filter)
        if skill.name not in present
    ]
    return dataclasses.replace(spec, skills=[*spec.skills, *extra]) if extra else spec


def apply_session_role(
    spec: AgentSpec, labels: Mapping[str, str] | None, workspace: str | None
) -> AgentSpec:
    """Overlay the role the session's labels name; mark a fork and a force-released holder."""
    from omnigent.runtime.prompt import (
        append_framework_instructions,
        session_identity_instructions,
    )

    overlay = load_session_role_overlay(labels, workspace)
    spec = apply_role_overlay(spec, overlay) if overlay is not None else spec
    spec = apply_library_skills(spec, labels, workspace)
    lines = [
        line
        for line in session_identity_instructions(labels)
        if line not in (spec.instructions or "")
    ]
    if not lines:
        return spec
    return dataclasses.replace(
        spec, instructions=append_framework_instructions(spec.instructions, lines)
    )

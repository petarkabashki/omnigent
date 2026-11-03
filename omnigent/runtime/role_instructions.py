"""Per-role instructions for sessions addressed by a role alias.

A session aliased ``Architect`` loads ``<workspace>/.omnigent/roles/Architect.md``,
falling back to ``~/.omnigent/roles/Architect.md``. The text layers after the
agent's own instructions, so one agent spec can serve several roles.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from omnigent.session_alias_client import ALIAS_PATTERN
from omnigent.spec.parser import _FRONTMATTER_RE

_logger = logging.getLogger(__name__)

ROLES_DIR_PARTS = (".omnigent", "roles")
MAX_ROLE_FILE_BYTES = 64 * 1024


def role_file_candidates(
    alias: str, workspace: str | None, home: Path | None = None
) -> list[Path]:
    """Directories searched for ``<alias>.md``, most specific first."""
    dirs: list[Path] = []
    if workspace:
        dirs.append(Path(workspace).expanduser().joinpath(*ROLES_DIR_PARTS))
    dirs.append((home or Path.home()).joinpath(*ROLES_DIR_PARTS))
    return dirs


def _find_role_file(directory: Path, alias: str) -> Path | None:
    """Return ``directory/<alias>.md``, matching the alias case-insensitively."""
    exact = directory / f"{alias}.md"
    if exact.is_file():
        return exact
    if not directory.is_dir():
        return None
    wanted = f"{alias}.md".casefold()
    for entry in sorted(directory.iterdir()):
        if entry.name.casefold() == wanted and entry.is_file():
            return entry
    return None


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


def load_role_instructions(
    alias: str | None, workspace: str | None, *, home: Path | None = None
) -> str | None:
    """Return the role section for ``alias``, or ``None`` when there is no role file.

    :param alias: The session's role alias (label ``omnigent.alias``).
    :param workspace: The session's workspace; its role file wins over the home one.
    :param home: Home directory override, for tests.
    :returns: The role text under a header naming the alias and source file.
    """
    if not alias or not ALIAS_PATTERN.match(alias):
        return None
    for directory in role_file_candidates(alias, workspace, home):
        try:
            path = _find_role_file(directory, alias)
            if path is None:
                continue
            raw = path.read_bytes()[:MAX_ROLE_FILE_BYTES]
        except OSError as exc:
            _logger.warning("Could not read role instructions in %s: %s", directory, exc)
            continue
        meta, body = split_role_file(raw.decode("utf-8", errors="replace"))
        text = body.strip()
        if not text:
            return None
        description = meta.get("description")
        summary = f" ({description.strip()})" if isinstance(description, str) else ""
        return f"## Role: {alias}{summary}\n\nRole instructions from {path}:\n\n{text}"
    return None

"""Named executor profiles from ``~/.omnigent/executors.yaml``.

A profile bundles the choices that decide who runs a session::

    claude-opus:
      harness: claude-native
      model: opus
      reasoning_effort: high
      host_needs: [gpu]
      description: Deep review work

A session create that names ``executor`` gets the profile's values for every
field the caller left unset; explicit request fields always win.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError

EXECUTORS_FILE_PARTS = (".omnigent", "executors.yaml")
EXECUTOR_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
MAX_EXECUTORS_FILE_BYTES = 256 * 1024

_STRING_KEYS = (
    "agent_id",
    "harness",
    "model",
    "reasoning_effort",
    "host_preference",
    "description",
)
_ALLOWED_KEYS = frozenset((*_STRING_KEYS, "host_needs"))
# Profile key -> create-body field; ``harness`` and ``model`` are overrides there.
_BODY_FIELDS = {
    "agent_id": "agent_id",
    "harness": "harness_override",
    "model": "model_override",
    "reasoning_effort": "reasoning_effort",
    "host_needs": "host_needs",
    "host_preference": "host_preference",
}

_BodyT = TypeVar("_BodyT", bound=BaseModel)


@dataclasses.dataclass(frozen=True)
class ExecutorProfile:
    """One named executor bundle; ``None``/empty fields are left to the caller."""

    name: str
    agent_id: str | None = None
    harness: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    host_needs: tuple[str, ...] = ()
    host_preference: str | None = None
    description: str | None = None


_cache: dict[Path, tuple[tuple[int, int, int], dict[str, ExecutorProfile]]] = {}


def executors_path(home: Path | None = None) -> Path:
    """Location of the user's executor profile file."""
    return (home or Path.home()).joinpath(*EXECUTORS_FILE_PARTS)


def _invalid(path: Path, message: str) -> OmnigentError:
    return OmnigentError(f"{path}: {message}", code=ErrorCode.INVALID_INPUT)


def _parse_profile(path: Path, name: str, raw: object) -> ExecutorProfile:
    if not EXECUTOR_NAME_PATTERN.match(name):
        raise _invalid(path, f"executor name {name!r} must match {EXECUTOR_NAME_PATTERN.pattern}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _invalid(path, f"executor {name!r} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in _ALLOWED_KEYS)
    if unknown:
        raise _invalid(
            path,
            f"executor {name!r} has unknown keys {unknown}; allowed: {sorted(_ALLOWED_KEYS)}",
        )
    fields: dict[str, Any] = {}  # type: ignore[explicit-any]
    for key in _STRING_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise _invalid(path, f"executor {name!r}: {key} must be a nonempty string")
        fields[key] = value.strip()
    needs = raw.get("host_needs")
    if needs is not None:
        if not isinstance(needs, list) or not all(isinstance(n, str) and n.strip() for n in needs):
            raise _invalid(path, f"executor {name!r}: host_needs must be a list of strings")
        fields["host_needs"] = tuple(n.strip() for n in needs)
    return ExecutorProfile(name=name, **fields)


def parse_executors(text: str, path: Path) -> dict[str, ExecutorProfile]:
    """Parse and validate the YAML text of an executors file."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _invalid(path, f"unreadable YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _invalid(path, "top level must map executor names to profiles")
    return {str(name): _parse_profile(path, str(name), raw) for name, raw in data.items()}


def load_executor_profiles(home: Path | None = None) -> dict[str, ExecutorProfile]:
    """Load all profiles, re-reading the file only when its mtime changes.

    :returns: Name -> profile; empty when the file does not exist.
    :raises OmnigentError: ``INVALID_INPUT`` for unknown keys or bad values.
    """
    path = executors_path(home)
    try:
        stat = path.stat()
    except OSError:
        _cache.pop(path, None)
        return {}
    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    if stat.st_size > MAX_EXECUTORS_FILE_BYTES:
        raise _invalid(path, f"file exceeds {MAX_EXECUTORS_FILE_BYTES} bytes")
    try:
        text = path.read_text()
    except OSError as exc:
        raise _invalid(path, f"unreadable: {exc}") from exc
    profiles = parse_executors(text, path)
    _cache[path] = (signature, profiles)
    return profiles


def get_executor_profile(name: str, home: Path | None = None) -> ExecutorProfile:
    """Return the named profile or raise ``INVALID_INPUT`` listing the known names."""
    profiles = load_executor_profiles(home)
    profile = profiles.get(name)
    if profile is None:
        known = ", ".join(sorted(profiles)) or "none configured"
        raise OmnigentError(
            f"Unknown executor {name!r}; known executors: {known}", code=ErrorCode.INVALID_INPUT
        )
    return profile


def apply_executor_profile(body: _BodyT, profile: ExecutorProfile) -> _BodyT:
    """Return ``body`` with the profile's values filled into fields the caller left unset.

    Fields the body model does not declare are skipped. An explicit ``host_id``
    bypasses placement, so the profile's host fields are not applied then.
    """
    declared = body.__class__.model_fields
    explicit_host = getattr(body, "host_id", None) is not None
    fills: dict[str, object] = {}
    for key, field in _BODY_FIELDS.items():
        value = getattr(profile, key)
        if explicit_host and field in ("host_needs", "host_preference"):
            continue
        if value and field in declared and field not in body.model_fields_set:
            fills[field] = list(value) if isinstance(value, tuple) else value
    return body.model_copy(update=fills) if fills else body

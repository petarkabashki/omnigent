"""Host-level runner admission limits read from ``host.resources`` in config.yaml.

Example::

    host:
      resources:
        max_runners: 4
        min_mem_avail_mb: 2048
        session_memory_max: 6G
        session_memory_high: 5G
        session_cpu_quota: 200%

Every key is optional. An invalid value is logged and treated as unset so a typo
never stops the host.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omnigent.host.identity import host_config_path

_logger = logging.getLogger(__name__)

MEMINFO_PATH = Path("/proc/meminfo")

REASON_SLOTS = "slots"
REASON_MEM = "mem"

_UNIT_SHIFT = {"K": 10, "M": 20, "G": 30, "T": 40}
_SIZE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"^\s*(\d+)\s*%\s*$")
_MIB = 1 << 20


@dataclass(frozen=True)
class HostResourceConfig:
    """Admission limits and per-runner defaults; ``None`` means unset.

    :param max_runners: Concurrent runner cap, e.g. ``4``.
    :param min_mem_avail_mb: Refuse launches when MemAvailable is below this many MiB.
    :param session_memory_max: Default per-runner ``MemoryMax`` in bytes.
    :param session_memory_high: Default per-runner ``MemoryHigh`` in bytes.
    :param session_cpu_quota: Default per-runner ``CPUQuota`` percent, e.g. ``200``.
    """

    max_runners: int | None = None
    min_mem_avail_mb: int | None = None
    session_memory_max: int | None = None
    session_memory_high: int | None = None
    session_cpu_quota: int | None = None


def parse_memory_size(value: object) -> int:
    """Parse ``"6G"``, ``"512M"``, ``"1.5G"`` or an int into bytes (binary units).

    :raises ValueError: On a malformed or non-positive value.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid memory size: {value!r}")
    if isinstance(value, int):
        size = value
    else:
        match = _SIZE_PATTERN.match(str(value))
        if match is None:
            raise ValueError(f"invalid memory size: {value!r}")
        unit = match.group(2).upper()
        size = int(float(match.group(1)) * (1 << _UNIT_SHIFT.get(unit, 0)))
    if size <= 0:
        raise ValueError(f"memory size must be positive: {value!r}")
    return size


def parse_cpu_quota(value: object) -> int:
    """Parse ``"200%"`` (or int ``200``) into a percent.

    :raises ValueError: On a malformed or non-positive value.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid CPU quota: {value!r}")
    if isinstance(value, int):
        percent = value
    else:
        match = _PERCENT_PATTERN.match(str(value))
        if match is None:
            raise ValueError(f"invalid CPU quota: {value!r}")
        percent = int(match.group(1))
    if percent <= 0:
        raise ValueError(f"CPU quota must be positive: {value!r}")
    return percent


def _parse_positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"expected a positive integer: {value!r}")
    return value


_FIELD_PARSERS = {
    "max_runners": _parse_positive_int,
    "min_mem_avail_mb": _parse_positive_int,
    "session_memory_max": parse_memory_size,
    "session_memory_high": parse_memory_size,
    "session_cpu_quota": parse_cpu_quota,
}


def load_host_resource_config(path: Path | None = None) -> HostResourceConfig:
    """Read ``host.resources`` from the host config; unreadable or invalid means unset.

    :param path: Config file; defaults to the host's ``config.yaml``.
    """
    try:
        with open(host_config_path(path)) as handle:
            cfg = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return HostResourceConfig()
    except (OSError, yaml.YAMLError) as exc:
        _logger.warning("Ignoring host.resources: cannot read config: %s", exc)
        return HostResourceConfig()
    host_section = cfg.get("host") if isinstance(cfg, dict) else None
    section = host_section.get("resources") if isinstance(host_section, dict) else None
    if section is None:
        return HostResourceConfig()
    if not isinstance(section, dict):
        _logger.warning("Ignoring host.resources: expected a mapping, got %r", section)
        return HostResourceConfig()
    values: dict[str, int] = {}
    for key, raw in section.items():
        parser = _FIELD_PARSERS.get(key)
        if parser is None:
            _logger.warning("Ignoring unknown host.resources key %r", key)
            continue
        try:
            values[key] = parser(raw)
        except ValueError as exc:
            _logger.warning("Ignoring host.resources.%s: %s", key, exc)
    return HostResourceConfig(**values)


def available_memory_bytes(meminfo_path: Path = MEMINFO_PATH) -> int | None:
    """Return ``MemAvailable`` in bytes, or ``None`` when it cannot be read."""
    try:
        text = meminfo_path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def admission_refusal(
    config: HostResourceConfig,
    running_count: int,
    available: int | None,
    *,
    host_name: str = "this host",
) -> tuple[str, str] | None:
    """Return ``(reason, message)`` when a new runner must be refused, else ``None``.

    :param config: The host's limits.
    :param running_count: Live runners, excluding any the launch supersedes.
    :param available: MemAvailable in bytes; ``None`` skips the memory check.
    :param host_name: Name used in the message.
    :returns: ``reason`` is :data:`REASON_SLOTS` or :data:`REASON_MEM`.
    """
    if config.max_runners is not None and running_count >= config.max_runners:
        return (
            REASON_SLOTS,
            f"host {host_name!r} is at its limit of {config.max_runners} runners",
        )
    if config.min_mem_avail_mb is not None and available is not None:
        floor = config.min_mem_avail_mb * _MIB
        if available < floor:
            return (
                REASON_MEM,
                f"host {host_name!r} has {available / (1 << 30):.1f} GiB available, "
                f"below its {floor / (1 << 30):.1f} GiB floor",
            )
    return None


RESOURCE_LABEL_PREFIX = "omnigent.resource."
USAGE_LABEL_PREFIX = "omnigent.usage."
_LIMIT_KEYS = ("memory_max", "memory_high", "cpu_quota")


def resource_limits_from_labels(labels: Mapping[str, str] | None) -> dict[str, str] | None:
    """Pick the ``omnigent.resource.*`` limits (memory_max/memory_high/cpu_quota) from labels.

    :returns: e.g. ``{"memory_max": "6G"}``, or ``None`` when no limit label is set.
    """
    if not labels:
        return None
    limits = {
        key: str(labels[RESOURCE_LABEL_PREFIX + key])
        for key in _LIMIT_KEYS
        if labels.get(RESOURCE_LABEL_PREFIX + key)
    }
    return limits or None


def usage_labels(usage: Mapping[str, Any]) -> dict[str, str]:
    """Map a runner usage sample to ``omnigent.usage.*`` label updates."""
    fields = (
        ("memory_peak_bytes", "memory_peak_bytes"),
        ("cpu_usage_usec", "cpu_usage_usec"),
        ("oom_kill_count", "oom_kills"),
    )
    return {
        USAGE_LABEL_PREFIX + label: str(usage[key])
        for key, label in fields
        if usage.get(key) is not None
    }

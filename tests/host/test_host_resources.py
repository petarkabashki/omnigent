"""Tests for host.resources parsing, loading and admission messages."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.host.host_resources import (
    HostResourceConfig,
    admission_refusal,
    available_memory_bytes,
    load_host_resource_config,
    parse_cpu_quota,
    parse_memory_size,
)

_GIB = 1 << 30


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("6G", 6 * _GIB),
        ("512M", 512 << 20),
        ("1.5G", int(1.5 * _GIB)),
        ("2GiB", 2 * _GIB),
        ("4k", 4096),
        (1024, 1024),
    ],
)
def test_parse_memory_size(value: object, expected: int) -> None:
    assert parse_memory_size(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "-1G", "0", 0, True, None, "5X"])
def test_parse_memory_size_rejects_invalid(value: object) -> None:
    with pytest.raises(ValueError):
        parse_memory_size(value)


def test_parse_cpu_quota() -> None:
    assert parse_cpu_quota("200%") == 200
    assert parse_cpu_quota(150) == 150
    for bad in ("200", "x%", "0%", True):
        with pytest.raises(ValueError):
            parse_cpu_quota(bad)


def test_load_config_reads_all_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "host:\n  resources:\n    max_runners: 4\n    min_mem_avail_mb: 2048\n"
        "    session_memory_max: 6G\n    session_memory_high: 5G\n"
        "    session_cpu_quota: 200%\n"
    )
    assert load_host_resource_config(path) == HostResourceConfig(
        max_runners=4,
        min_mem_avail_mb=2048,
        session_memory_max=6 * _GIB,
        session_memory_high=5 * _GIB,
        session_cpu_quota=200,
    )


def test_load_config_invalid_values_become_unset(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "host:\n  resources:\n    max_runners: lots\n    session_memory_max: 6G\n    bogus: 1\n"
    )
    assert load_host_resource_config(path) == HostResourceConfig(session_memory_max=6 * _GIB)


@pytest.mark.parametrize("text", [None, "host: 3\n", "host:\n  resources: nope\n", ": [\n"])
def test_load_config_missing_or_malformed_is_empty(tmp_path: Path, text: str | None) -> None:
    path = tmp_path / "config.yaml"
    if text is not None:
        path.write_text(text)
    assert load_host_resource_config(path) == HostResourceConfig()


def test_available_memory_bytes(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 100 kB\nMemAvailable:    2048 kB\n")
    assert available_memory_bytes(meminfo) == 2048 * 1024
    meminfo.write_text("MemTotal: 100 kB\n")
    assert available_memory_bytes(meminfo) is None
    assert available_memory_bytes(tmp_path / "missing") is None


def test_admission_refusal_messages() -> None:
    config = HostResourceConfig(max_runners=4, min_mem_avail_mb=2048)
    assert admission_refusal(config, 4, 8 * _GIB, host_name="hpz4") == (
        "slots",
        "host 'hpz4' is at its limit of 4 runners",
    )
    assert admission_refusal(config, 1, int(1.2 * _GIB), host_name="hpz4") == (
        "mem",
        "host 'hpz4' has 1.2 GiB available, below its 2.0 GiB floor",
    )
    assert admission_refusal(config, 3, 8 * _GIB) is None
    # Unknown memory (non-Linux) and an empty config never refuse.
    assert admission_refusal(config, 3, None) is None
    assert admission_refusal(HostResourceConfig(), 99, 0) is None

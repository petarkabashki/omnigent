"""A fake ``ag-fleet`` executable that emits the frozen ``place`` contract shapes."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

_SCRIPT = f"""#!{sys.executable}
import json, pathlib, sys, time

here = pathlib.Path(__file__).parent
scenario = json.loads((here / "scenario.json").read_text())
args = sys.argv[1:]
stdin = sys.stdin.read() if "-" in args else ""
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
mode = scenario["mode"]
if "--release" in args:
    rid = args[args.index("--release") + 1]
    print(json.dumps({{"schema_version": 1, "ok": True, "released": True, "reservation_id": rid}}))
    sys.exit(0)
if mode == "hang":
    time.sleep(60)
if mode == "badjson":
    print("not json")
    sys.exit(0)
if mode == "bad_schema":
    print(json.dumps({{"schema_version": 2, "ok": True}}))
    sys.exit(0)
if mode == "malformed":
    print(json.dumps({{"schema_version": 1, "ok": True, "host": "box",
        "reservation_id": "res_0123456789abcdef"}}))
    sys.exit(0)
if mode == "usage":
    print(json.dumps({{"schema_version": 1, "ok": False, "code": "bad_ttl", "reason": "ttl"}}))
    sys.exit(2)
if mode == "refuse":
    print(json.dumps({{"schema_version": 1, "ok": False, "code": "quiet_window",
        "reason": "optiplex is quiet 00:00-07:00 UTC", "candidates": []}}))
    sys.exit(1)
print(json.dumps({{
    "schema_version": 1, "ok": True, "host": "box", "host_id": scenario["host_id"],
    "reservation_id": "res_0123456789abcdef", "expires_at": "2026-11-03T12:00:00Z",
    "ttl_s": 120, "strategy": "least_loaded", "reason": scenario.get("reason", "ok"),
    "request_id": args[args.index("--request-id") + 1],
    "candidates": [{{"host": "box", "host_id": scenario["host_id"], "rank": 1, "score": None,
        "eligible": True, "reason": "ok"}}],
}}))
"""


def install_fake_ag_fleet(directory: Path, monkeypatch, mode: str = "ok", **scenario) -> Path:
    """
    Write the fake binary into ``directory`` and select it via the environment.

    :param directory: Empty directory that also holds ``scenario.json`` and ``calls.jsonl``.
    :param monkeypatch: pytest monkeypatch used to set ``OMNIGENT_AG_FLEET_BIN``.
    :param mode: ``ok``, ``refuse``, ``usage``, ``hang``, ``badjson``, ``malformed``, etc.
    :param scenario: Extra reply fields, e.g. ``host_id`` or ``reason``.
    :returns: The directory, for reading ``calls.jsonl``.
    """
    binary = directory / "ag-fleet"
    binary.write_text(_SCRIPT)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (directory / "scenario.json").write_text(json.dumps({"mode": mode, **scenario}))
    monkeypatch.setenv("OMNIGENT_AG_FLEET_BIN", str(binary))
    return directory


def read_calls(directory: Path) -> list[dict]:
    """Return the recorded invocations (``args`` and ``stdin``) in order."""
    log = directory / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

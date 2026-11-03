"""Client for the ``ag-fleet place`` host-placement binary."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omnigent.db.utils import now_epoch
from omnigent.stores.host_store import Host, host_is_live

_logger = logging.getLogger(__name__)

AG_FLEET_BIN_ENV_VAR = "OMNIGENT_AG_FLEET_BIN"
DEFAULT_AG_FLEET_BIN = "ag-fleet"
PLACEMENT_TIMEOUT_S = 15.0
PLACEMENT_SCHEMA_VERSION = 1
PLACEMENT_REASON_LABEL_KEY = "omnigent.placement_reason"
# ag-fleet request id, to match a session to ag-fleet's own logs.
PLACEMENT_REQUEST_ID_LABEL_KEY = "omnigent.placement_request_id"
DEFAULT_RESERVATION_TTL_S = 120
MAX_REQUEST_ID_LENGTH = 64

# ag-fleet exit-2 codes: a bug in how omnigent invoked the binary, not user error.
_USAGE_ERROR_EXIT_CODE = 2
_REFUSAL_EXIT_CODE = 1


class PlacementError(Exception):
    """Placement could not produce a decision (binary missing, timeout, bad reply)."""

    def __init__(self, message: str, *, internal: bool = False) -> None:
        """
        :param message: Text safe to show the caller, e.g. ``"placement timed out"``.
        :param internal: ``True`` for an omnigent-side bug (usage error, bad snapshot).
        """
        super().__init__(message)
        self.message = message
        self.internal = internal


@dataclass(frozen=True)
class PlacementCandidate:
    """One host considered by ag-fleet, with its verdict."""

    host: str
    host_id: str | None
    rank: int | None
    score: float | None
    eligible: bool
    reason: str


@dataclass(frozen=True)
class PlacementResult:
    """A successful placement and the reservation holding the capacity."""

    host: str
    host_id: str
    reservation_id: str
    expires_at: str | int | float
    ttl_s: int
    strategy: str
    reason: str
    request_id: str
    candidates: tuple[PlacementCandidate, ...] = field(default_factory=tuple)


class PlacementRefusedError(Exception):
    """ag-fleet declined to place the session (exit code 1)."""

    def __init__(
        self, code: str, reason: str, candidates: tuple[PlacementCandidate, ...] = ()
    ) -> None:
        """
        :param code: ag-fleet refusal code, e.g. ``"no_eligible_host"``.
        :param reason: ag-fleet's explanation, passed to the caller verbatim.
        :param candidates: Per-host verdicts, possibly empty.
        """
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.candidates = candidates


def ag_fleet_binary() -> str:
    """Return the configured ag-fleet executable name or path."""
    return os.environ.get(AG_FLEET_BIN_ENV_VAR) or DEFAULT_AG_FLEET_BIN


def build_hosts_snapshot(
    hosts: Iterable[Host], running_sessions_by_host: Mapping[str, int]
) -> list[dict[str, Any]]:
    """
    Build the ``--hosts-json`` rows from the hosts table.

    :param hosts: Hosts visible to the caller.
    :param running_sessions_by_host: Count of active sessions per ``host_id``.
    :returns: Rows with ``configured_harnesses`` flattened to the available names.
    """
    now = now_epoch()
    return [
        {
            "host_id": host.host_id,
            "name": host.name,
            "status": "online" if host_is_live(host, now=now) else "offline",
            "configured_harnesses": sorted(
                name for name, state in (host.configured_harnesses or {}).items() if state is True
            ),
            "running_sessions": running_sessions_by_host.get(host.host_id, 0),
        }
        for host in hosts
        if host.sandbox_provider is None
    ]


async def _run(args: Sequence[str], stdin_data: bytes | None) -> tuple[int, dict[str, Any]]:
    """Run ag-fleet and return ``(exit_code, parsed stdout JSON object)``."""
    binary = ag_fleet_binary()
    if os.sep not in binary and shutil.which(binary) is None:
        raise PlacementError(
            f"ag-fleet binary {binary!r} not found; install it or set {AG_FLEET_BIN_ENV_VAR}"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PlacementError(
            f"ag-fleet binary {binary!r} could not be run ({exc}); check {AG_FLEET_BIN_ENV_VAR}"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_data), timeout=PLACEMENT_TIMEOUT_S
        )
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise PlacementError("placement timed out") from exc
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    exit_code = process.returncode if process.returncode is not None else -1
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        _logger.error(
            "ag-fleet returned non-JSON output (exit=%s): %r stderr=%r",
            exit_code,
            stdout[:200],
            stderr[:200],
        )
        raise PlacementError("placement failed (invalid reply)", internal=True) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PLACEMENT_SCHEMA_VERSION:
        _logger.error("ag-fleet reply has unsupported schema: %r", stdout[:200])
        raise PlacementError("placement failed (unsupported reply)", internal=True)
    return exit_code, payload


def _parse_candidates(raw: object) -> tuple[PlacementCandidate, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        PlacementCandidate(
            host=str(item.get("host", "")),
            host_id=item.get("host_id"),
            rank=item.get("rank"),
            score=item.get("score"),
            eligible=bool(item.get("eligible")),
            reason=str(item.get("reason", "")),
        )
        for item in raw
        if isinstance(item, dict)
    )


async def place_host(
    *,
    needs: Sequence[str],
    snapshot: Sequence[Mapping[str, Any]],
    request_id: str,
    host: str | None = None,
    profile: str | None = None,
    ttl_s: int = DEFAULT_RESERVATION_TTL_S,
) -> PlacementResult:
    """
    Ask ag-fleet to pick a host and reserve capacity on it.

    :param needs: Capabilities the host must have, e.g. ``["claude-sdk"]``.
    :param snapshot: Rows from :func:`build_hosts_snapshot`.
    :param request_id: Create-attempt id, at most 64 chars.
    :param host: Preferred host name, passed as ``--host``.
    :param profile: Optional ag-fleet profile name.
    :param ttl_s: Reservation lifetime in seconds.
    :returns: The placement with its reservation.
    :raises PlacementRefusedError: ag-fleet refused (exit 1).
    :raises PlacementError: Binary missing, timeout, usage error, or malformed reply.
    """
    args = ["place"]
    for need in needs:
        args += ["--needs", need]
    if host:
        args += ["--host", host]
    if profile:
        args += ["--profile", profile]
    args += [
        "--ttl",
        str(ttl_s),
        "--request-id",
        request_id[:MAX_REQUEST_ID_LENGTH],
        "--hosts-json",
        "-",
        "--json",
    ]
    exit_code, payload = await _run(args, json.dumps(list(snapshot)).encode())
    if exit_code == _USAGE_ERROR_EXIT_CODE:
        _logger.error(
            "ag-fleet place usage error (omnigent bug): code=%s reason=%s",
            payload.get("code"),
            payload.get("reason"),
        )
        raise PlacementError("placement failed (internal error)", internal=True)
    if exit_code == _REFUSAL_EXIT_CODE or payload.get("ok") is False:
        raise PlacementRefusedError(
            str(payload.get("code", "no_eligible_host")),
            str(payload.get("reason", "")),
            _parse_candidates(payload.get("candidates")),
        )
    try:
        if exit_code != 0 or payload.get("ok") is not True:
            raise KeyError("ok")
        return PlacementResult(
            host=str(payload["host"]),
            host_id=str(payload["host_id"]),
            reservation_id=str(payload["reservation_id"]),
            expires_at=payload["expires_at"],
            ttl_s=int(payload["ttl_s"]),
            strategy=str(payload["strategy"]),
            reason=str(payload.get("reason", "")),
            request_id=str(payload.get("request_id", request_id)),
            candidates=_parse_candidates(payload.get("candidates")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _logger.error("ag-fleet place reply is malformed (exit=%s): %r", exit_code, payload)
        stray_reservation_id = payload.get("reservation_id")
        if isinstance(stray_reservation_id, str):
            await release_reservation(stray_reservation_id)
        raise PlacementError("placement failed (invalid reply)", internal=True) from exc


async def release_reservation(reservation_id: str) -> bool:
    """
    Release a placement reservation; never raises.

    :param reservation_id: Id from :attr:`PlacementResult.reservation_id`.
    :returns: ``True`` when ag-fleet reported the reservation released.
    """
    try:
        _, payload = await _run(["place", "--release", reservation_id, "--json"], None)
    except Exception:
        _logger.warning(
            "Failed to release placement reservation %s", reservation_id, exc_info=True
        )
        return False
    if payload.get("ok") is not True:
        _logger.warning("ag-fleet refused to release %s: %s", reservation_id, payload.get("code"))
        return False
    return payload.get("released") is True

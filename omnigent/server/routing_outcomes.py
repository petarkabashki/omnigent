"""Routing outcome stream: what happened after a routing decision.

Events are appended to ``<data dir>/routing/outcomes.jsonl`` beside
``decisions.jsonl`` (same 0600 ``O_APPEND`` path), keyed by ``decision_id``.
The decision log stays write-once; every later fact about the session lands
here. Emitters know only a session id, so the decision id is resolved from the
decision log; a session with no logged decision emits nothing (shadow routing
off). Emitting never raises.

Events (``event`` field), each with ``at`` (epoch seconds):

* ``override``: ``kind`` (interrupt, harness_change, model_change,
  host_change, reset, rerun), ``actor``. Only ``actor="owner"`` counts.
* ``cost``: cumulative snapshot with ``usd_billed`` (``total_cost_usd``),
  ``usd_policy`` (``policy_cost_usd`` or null) and ``session_usage``.
* ``ended``: a ``cost`` snapshot taken when the session stops or is deleted.
* ``check_declared``: ``check_ref``, written before any result exists.
* ``check``: ``status`` (passed, failed, error), ``passed`` (null on error).
* ``accept``: the owner's explicit accept gesture.

``cost`` and ``ended`` are cumulative and may append out of order, so readers
take the max per ``decision_id`` (:func:`summarize_outcomes`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from omnigent.server.shadow_routing import (
    SCHEMA_VERSION,
    append_jsonl_line,
    decisions_path,
    iter_jsonl_records,
)

_logger = logging.getLogger(__name__)

OVERRIDE_KINDS = frozenset(
    {"interrupt", "harness_change", "model_change", "host_change", "reset", "rerun"}
)
CHECK_STATUSES = frozenset({"passed", "failed", "error"})
# ``session_usage`` keys copied into a cost snapshot so a reader can compare the
# stream with the stored totals, including the base a native reset rebased on.
_SNAPSHOT_KEYS = (
    "total_cost_usd",
    "policy_cost_usd",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "context_reset_base",
)


def outcomes_path() -> Path:
    """Return ``<data dir>/routing/outcomes.jsonl`` (beside the decision log)."""
    return decisions_path().with_name("outcomes.jsonl")


class _DecisionIndex:
    """Session id to decision id, built by reading ``decisions.jsonl`` incrementally."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._offset = 0
        self._by_session: dict[str, str] = {}

    def lookup(self, session_id: str, path: Path) -> str | None:
        with self._lock:
            try:
                size = path.stat().st_size
            except OSError:
                return None
            if path != self._path or size < self._offset:
                self._path, self._offset, self._by_session = path, 0, {}
            if size > self._offset:
                self._read_new(path)
            return self._by_session.get(session_id)

    def _read_new(self, path: Path) -> None:
        with path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self._offset += len(complete)
        for line in complete.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            session_id, decision_id = record.get("session_id"), record.get("decision_id")
            if isinstance(session_id, str) and isinstance(decision_id, str):
                self._by_session.setdefault(session_id, decision_id)


# custom-lint: disable-next=workspace-scoped-cache -- tails one append-only log
_decision_index = _DecisionIndex()


def decision_id_for_session(session_id: str, path: Path | None = None) -> str | None:
    """Return the decision id logged for *session_id*, or ``None``."""
    return _decision_index.lookup(session_id, path or decisions_path())


def append_outcome(
    decision_id: str,
    event: str,
    fields: Mapping[str, Any],  # type: ignore[explicit-any]  # JSON fields
    *,
    session_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one outcome record for *decision_id* (``at`` defaults to now)."""
    now = time.time()
    record = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "session_id": session_id,
        "event": event,
        "at": now,
        **fields,
    }
    append_jsonl_line(
        path or outcomes_path(), json.dumps(record, default=str, separators=(",", ":"))
    )


def record_session_outcome(
    session_id: str,
    event: str,
    fields: Mapping[str, Any] | None = None,  # type: ignore[explicit-any]  # JSON fields
    *,
    path: Path | None = None,
) -> bool:
    """Append *event* for the decision logged for *session_id*; never raises.

    :returns: ``True`` when a record was written, ``False`` when the session has
        no logged decision or the write failed.
    """
    try:
        decision_id = decision_id_for_session(
            session_id, path.with_name("decisions.jsonl") if path else None
        )
        if decision_id is None:
            return False
        append_outcome(decision_id, event, fields or {}, session_id=session_id, path=path)
        return True
    except Exception:
        _logger.warning("routing outcome %r not recorded", event, exc_info=True)
        return False


def record_override(session_id: str, kind: str, actor: str, *, path: Path | None = None) -> bool:
    """Record an override of the router's choice. Only ``actor="owner"`` counts."""
    if kind not in OVERRIDE_KINDS:
        raise ValueError(f"unknown override kind {kind!r}")
    return record_session_outcome(
        session_id, "override", {"kind": kind, "actor": actor}, path=path
    )


def _usage_fields(usage: Mapping[str, Any] | None) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON fields
    """Cost-snapshot fields from a ``session_usage`` mapping."""
    usage = usage or {}
    billed, policy = usage.get("total_cost_usd"), usage.get("policy_cost_usd")
    return {
        "usd_billed": billed,
        "usd_policy": policy,
        "session_usage": {key: usage[key] for key in _SNAPSHOT_KEYS if key in usage},
    }


def record_cost(
    session_id: str,
    usage: Mapping[str, Any] | None,  # type: ignore[explicit-any]  # JSON fields
    *,
    context_epoch: int | None = None,
    path: Path | None = None,
) -> bool:
    """Record a cumulative cost snapshot of *usage* (the stored ``session_usage``)."""
    fields = _usage_fields(usage)
    if context_epoch is not None:
        fields["context_epoch"] = context_epoch
    return record_session_outcome(session_id, "cost", fields, path=path)


def record_ended(
    session_id: str,
    usage: Mapping[str, Any] | None,  # type: ignore[explicit-any]  # JSON fields
    *,
    reason: str,
    path: Path | None = None,
) -> bool:
    """Record that the session stopped or was deleted, with its final cost snapshot.

    ``usd_billed`` is always present; it is null when the session was never priced.
    """
    fields = {**_usage_fields(usage), "reason": reason}
    return record_session_outcome(session_id, "ended", fields, path=path)


def record_accept(session_id: str, actor: str, *, path: Path | None = None) -> bool:
    """Record the explicit accept gesture (live success only; never replayable)."""
    return record_session_outcome(session_id, "accept", {"actor": actor}, path=path)


def record_check_declared(decision_id: str, check_ref: str, *, path: Path | None = None) -> None:
    """Record that a check was declared, before any result exists."""
    append_outcome(decision_id, "check_declared", {"check_ref": check_ref}, path=path)


def record_check(
    decision_id: str,
    status: str,
    check_ref: str,
    *,
    path: Path | None = None,
) -> None:
    """Record a check result. ``passed`` is true/false for passed/failed, null for error."""
    if status not in CHECK_STATUSES:
        raise ValueError(f"unknown check status {status!r}")
    passed = None if status == "error" else status == "passed"
    append_outcome(
        decision_id,
        "check",
        {"status": status, "passed": passed, "check_ref": check_ref},
        path=path,
    )


# ── Readers ────────────────────────────────────────────────────────────────


def iter_outcomes(path: Path | None = None) -> Iterator[dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Yield outcome records, skipping blank or corrupt lines."""
    return iter_jsonl_records(path or outcomes_path())


def _max_number(current: float | None, value: Any) -> float | None:  # type: ignore[explicit-any]  # JSON field
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return current
    return value if current is None else max(current, value)


def summarize_outcomes(path: Path | None = None) -> dict[str, dict[str, Any]]:  # type: ignore[explicit-any]  # JSON record
    """Reduce the stream to one summary per ``decision_id``.

    Cumulative fields (``usd_billed``, ``usd_policy``, ``ended_at``,
    ``accept_at``) take the max, so append order does not matter. ``check`` and
    ``check_declared`` keep the latest by ``at``. ``overrides`` lists every
    override record.
    """
    summaries: dict[str, dict[str, Any]] = {}  # type: ignore[explicit-any]
    for record in iter_outcomes(path):
        decision_id = record.get("decision_id")
        if not isinstance(decision_id, str):
            continue
        summary = summaries.setdefault(
            decision_id,
            {
                "session_id": None,
                "usd_billed": None,
                "usd_policy": None,
                "ended_at": None,
                "accept_at": None,
                "check_declared": None,
                "check": None,
                "overrides": [],
            },
        )
        summary["session_id"] = summary["session_id"] or record.get("session_id")
        event, at = record.get("event"), record.get("at")
        if event in ("cost", "ended"):
            summary["usd_billed"] = _max_number(summary["usd_billed"], record.get("usd_billed"))
            summary["usd_policy"] = _max_number(summary["usd_policy"], record.get("usd_policy"))
            if event == "ended":
                summary["ended_at"] = _max_number(summary["ended_at"], at)
        elif event == "accept":
            summary["accept_at"] = _max_number(summary["accept_at"], at)
        elif event == "override":
            summary["overrides"].append(record)
        elif event in ("check", "check_declared"):
            latest = summary[event]
            if latest is None or (at or 0) >= (latest.get("at") or 0):
                summary[event] = record
    return summaries


def usage_gap(
    summary: Mapping[str, Any], session_usage: Mapping[str, Any]
) -> dict[str, float | None]:  # type: ignore[explicit-any]  # JSON record
    """Compare one :func:`summarize_outcomes` entry with the stored ``session_usage``.

    Each value is ``stream - stored`` in USD (negative means the stream
    under-counts), or ``None`` when either side is unpriced.
    """

    def gap(streamed: Any, stored: Any) -> float | None:  # type: ignore[explicit-any]
        if streamed is None or stored is None:
            return None
        return float(streamed) - float(stored)

    return {
        "usd_billed": gap(summary.get("usd_billed"), session_usage.get("total_cost_usd")),
        "usd_policy": gap(summary.get("usd_policy"), session_usage.get("policy_cost_usd")),
    }

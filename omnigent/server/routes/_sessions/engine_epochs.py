"""Per-engine spend for a session that switched engines in place."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.server.schemas import EngineEpochUsage


def _priced(usage: Mapping[str, Any]) -> float | None:
    cost = usage.get("total_cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None


def _input_tokens(usage: Mapping[str, Any]) -> int:
    return int(usage.get("input_tokens", 0) or 0) + int(
        usage.get("cache_read_input_tokens", 0) or 0
    )


def engine_epochs_from_switches(
    switches: Sequence[Mapping[str, Any]], current_usage: Mapping[str, Any]
) -> list[EngineEpochUsage] | None:
    """
    Split a session's own spend at each engine switch.

    :param switches: ``engine_change`` records of the session's switches, oldest
        first, each with ``from``/``to`` (harness, model) and ``usage_before``.
    :param current_usage: The session's current own ``session_usage``.
    :returns: One row per epoch, or ``None`` when the session never switched.
    """
    if not switches:
        return None
    rows: list[EngineEpochUsage] = []
    start: Mapping[str, Any] = {}
    for index, change in enumerate(switches):
        end = change.get("usage_before")
        end = end if isinstance(end, Mapping) else start
        rows.append(_row(index, change.get("from"), start, end))
        start = end
    rows.append(_row(len(switches), switches[-1].get("to"), start, current_usage))
    return rows


def _row(
    epoch: int, engine: object, start: Mapping[str, Any], end: Mapping[str, Any]
) -> EngineEpochUsage:
    engine = engine if isinstance(engine, Mapping) else {}
    end_cost = _priced(end)
    cost = None if end_cost is None else max(0.0, end_cost - (_priced(start) or 0.0))
    return EngineEpochUsage(
        epoch=epoch,
        harness=engine.get("harness"),
        model=engine.get("model"),
        cost_usd=cost,
        input_tokens=max(0, _input_tokens(end) - _input_tokens(start)),
        output_tokens=max(
            0, int(end.get("output_tokens", 0) or 0) - int(start.get("output_tokens", 0) or 0)
        ),
    )

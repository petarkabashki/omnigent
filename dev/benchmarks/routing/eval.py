#!/usr/bin/env python3
"""Router eval against the owner-decided bar (.omnigent/design/router-eval-plan.md).

Usage:
    uv run --no-sync python -m dev.benchmarks.routing.eval dataset \\
        --decisions decisions.jsonl [--outcomes outcomes.jsonl]
    uv run --no-sync python -m dev.benchmarks.routing.eval estimate \\
        --decisions decisions.jsonl --outcomes outcomes.jsonl
    uv run --no-sync python -m dev.benchmarks.routing.eval verdict --replay replay.jsonl
    uv run --no-sync python -m dev.benchmarks.routing.eval canary \\
        --decisions decisions.jsonl --outcomes outcomes.jsonl

Labels follow the owner's definitions: an override is an owner-initiated override
event; success is a passing check or an explicit accept, with no override; cost is
the billed USD summed over the decision's cost events, retries included.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MIN_PAIRED_TASKS = 300
MIN_CANARY_DECISIONS = 100
NON_INFERIORITY_MARGIN = 0.05
MAX_COST_INCREASE = 0.10
MIN_COST_SAVING = 0.15
OVERRIDE_GUARDRAIL = 0.05
RETRY_MARGIN = 0.25
Z_95 = 1.959963984540054
OVERRIDE_KINDS = frozenset(
    {"interrupt", "harness_change", "model_change", "host_change", "reset", "rerun"}
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON objects one per line, skipping blank lines."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------- dataset


@dataclass
class DecisionRow:
    """One routing decision, with every backend's proposal and its outcome labels."""

    decision_id: str
    site: str
    step_id: str | None
    namespace: str | None
    role: str | None
    arm: str
    exclusion: str | None
    pinned: list[str]
    actual: dict[str, Any]
    proposals: dict[str, dict[str, Any]]
    input_ref: str | None
    git_commit: str | None
    check_ref: str | None
    overridden: bool = False
    success: bool | None = None
    cost_usd: float | None = None
    override_kinds: list[str] = field(default_factory=list)
    dropped: bool = False
    check_errors: int = 0

    @property
    def offline_eligible(self) -> bool:
        """Whether the decision can be replayed offline and scored by a check."""
        return bool(
            self.check_ref
            and self.input_ref
            and self.git_commit
            and not self.pinned
            and self.exclusion is None
        )


def decision_key(record: dict[str, Any]) -> str:
    """Join key across backends; lane C records before L1 have one decision per session."""
    return record.get("decision_id") or f"session:{record['session_id']}"


def build_dataset(
    decisions: Iterable[dict[str, Any]], outcomes: Iterable[dict[str, Any]] = ()
) -> list[DecisionRow]:
    """Group per-backend decision records and attach outcome labels by decision id."""
    rows: dict[str, DecisionRow] = {}
    for record in decisions:
        key = decision_key(record)
        row = rows.get(key)
        if row is None:
            pinned = record.get("pinned") or []
            if isinstance(pinned, dict):
                pinned = sorted(name for name, is_pinned in pinned.items() if is_pinned)
            row = rows[key] = DecisionRow(
                decision_id=key,
                site=record.get("site", "session_create"),
                step_id=record.get("step_id") or record.get("session_id"),
                namespace=record.get("namespace") or record.get("project"),
                role=record.get("role") or record.get("alias"),
                arm=record.get("arm", "baseline"),
                exclusion=record.get("exclusion"),
                pinned=list(pinned),
                actual=record.get("actual") or {},
                proposals={},
                input_ref=record.get("input_ref"),
                git_commit=record.get("git_commit"),
                check_ref=record.get("check_ref"),
            )
        row.proposals[record.get("backend", "unknown")] = record.get("decision") or {}
        row.dropped = row.dropped or record.get("truncated") == "dropped"
    _attach_outcomes(rows, outcomes)
    return list(rows.values())


def _attach_outcomes(rows: dict[str, DecisionRow], outcomes: Iterable[dict[str, Any]]) -> None:
    checks: dict[str, tuple[str, bool]] = {}
    accepted: set[str] = set()
    for event in outcomes:
        row = rows.get(event.get("decision_id", ""))
        if row is None:
            continue
        kind = event.get("event")
        if kind == "override" and event.get("actor") == "owner":
            if event.get("kind") in OVERRIDE_KINDS:
                row.overridden = True
                row.override_kinds.append(event["kind"])
        elif kind == "check_declared":
            row.check_ref = row.check_ref or event.get("check_ref")
        elif kind == "check":
            row.check_ref = row.check_ref or event.get("check_ref")
            # A check that errored gave no answer, so it must not read as a failed step.
            if event.get("passed") is None:
                row.check_errors += 1
                continue
            at = str(event.get("at", ""))
            if row.decision_id not in checks or at >= checks[row.decision_id][0]:
                checks[row.decision_id] = (at, bool(event["passed"]))
        elif kind == "accept":
            accepted.add(row.decision_id)
        elif kind == "cost":
            row.cost_usd = (row.cost_usd or 0.0) + float(event.get("usd_billed", 0.0))
    for row in rows.values():
        has_check = row.decision_id in checks
        has_accept = row.decision_id in accepted
        if row.overridden:
            row.success = False if (has_check or has_accept) else None
        elif has_check or has_accept:
            row.success = has_accept or checks[row.decision_id][1]


def coverage_by_site(rows: Sequence[DecisionRow]) -> dict[str, dict[str, Any]]:
    """Per-site counts that every verdict must report alongside it."""
    report: dict[str, dict[str, Any]] = {}
    for row in rows:
        site = report.setdefault(
            row.site,
            {
                "decisions": 0,
                "with_check": 0,
                "with_success_label": 0,
                "overridden": 0,
                "pinned": 0,
                "excluded": 0,
                "offline_eligible": 0,
                "dropped": 0,
                "check_errors": 0,
            },
        )
        site["decisions"] += 1
        site["with_check"] += bool(row.check_ref)
        site["with_success_label"] += row.success is not None
        site["overridden"] += row.overridden
        site["pinned"] += bool(row.pinned)
        site["excluded"] += row.exclusion is not None
        site["offline_eligible"] += row.offline_eligible
        site["dropped"] += row.dropped
        site["check_errors"] += row.check_errors
    for site in report.values():
        site["check_coverage"] = site["with_check"] / site["decisions"]
    return report


# ---------------------------------------------------------------- estimate


def estimate_offline_cost(
    rows: Sequence[DecisionRow], router_backend: str, tasks: int = MIN_PAIRED_TASKS
) -> dict[str, dict[str, Any]]:
    """Per-site $ estimate for ``tasks`` paired replays, to send the owner before any run.

    The router arm is priced at the median observed cost of decisions that actually
    ran the model the router proposes; when none ran it, the baseline median is used
    and the site is flagged.
    """
    estimates: dict[str, dict[str, Any]] = {}
    for site in sorted({row.site for row in rows}):
        site_rows = [row for row in rows if row.site == site and row.cost_usd is not None]
        if not site_rows:
            estimates[site] = {"estimate_usd": None, "reason": "no billed cost observed"}
            continue
        cost_by_model: dict[str, list[float]] = {}
        for row in site_rows:
            cost_by_model.setdefault(str(row.actual.get("model")), []).append(row.cost_usd or 0.0)
        baseline_median = statistics.median(row.cost_usd or 0.0 for row in site_rows)
        router_costs: list[float] = []
        unpriced = 0
        for row in site_rows:
            proposed = (row.proposals.get(router_backend) or {}).get("model")
            observed = cost_by_model.get(str(proposed)) if proposed else None
            if observed:
                router_costs.append(statistics.median(observed))
            else:
                unpriced += 1
        router_median = statistics.median(router_costs) if router_costs else baseline_median
        per_task = baseline_median + router_median
        estimates[site] = {
            "tasks": tasks,
            "baseline_median_usd": round(baseline_median, 4),
            "router_median_usd": round(router_median, 4),
            "router_unpriced_fraction": round(unpriced / len(site_rows), 3),
            "estimate_usd": round(tasks * per_task * (1 + RETRY_MARGIN), 2),
            "observed_decisions": len(site_rows),
        }
    return estimates


# ---------------------------------------------------------------- statistics


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if total == 0:
        return (0.0, 1.0)
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    half_width = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total**2))
    return (
        max(0.0, centre - half_width / denominator),
        min(1.0, centre + half_width / denominator),
    )


def paired_difference_interval(
    both: int, only_first: int, only_second: int, neither: int, z: float = Z_95
) -> tuple[float, float, float]:
    """Newcombe (1998, method 10) interval for p_first - p_second on paired binary data."""
    total = both + only_first + only_second + neither
    if total == 0:
        raise ValueError("no pairs")
    first = (both + only_first) / total
    second = (both + only_second) / total
    delta = first - second
    low_first, high_first = wilson_interval(both + only_first, total, z)
    low_second, high_second = wilson_interval(both + only_second, total, z)
    margins = (
        (both + only_first)
        * (only_second + neither)
        * (both + only_second)
        * (only_first + neither)
    )
    phi = (both * neither - only_first * only_second) / math.sqrt(margins) if margins else 0.0
    lower = math.sqrt(
        max(
            0.0,
            (first - low_first) ** 2
            - 2 * phi * (first - low_first) * (high_second - second)
            + (high_second - second) ** 2,
        )
    )
    upper = math.sqrt(
        max(
            0.0,
            (high_first - first) ** 2
            - 2 * phi * (high_first - first) * (second - low_second)
            + (second - low_second) ** 2,
        )
    )
    return (delta, delta - lower, delta + upper)


def independent_difference_interval(
    successes_first: int, total_first: int, successes_second: int, total_second: int
) -> tuple[float, float, float]:
    """Newcombe (1998, method 10) hybrid score interval for two independent proportions."""
    first = successes_first / total_first
    second = successes_second / total_second
    low_first, high_first = wilson_interval(successes_first, total_first)
    low_second, high_second = wilson_interval(successes_second, total_second)
    delta = first - second
    return (
        delta,
        delta - math.sqrt((first - low_first) ** 2 + (high_second - second) ** 2),
        delta + math.sqrt((high_first - first) ** 2 + (second - low_second) ** 2),
    )


def bootstrap_cost_ratio(
    pairs: Sequence[tuple[float, float]], resamples: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """Geometric-mean router/baseline cost ratio with a 95% bootstrap CI.

    Each pair is ``(baseline_usd, router_usd)`` for one task. Tasks are resampled
    whole, and the mean is taken on per-task log ratios because run cost is
    heavy-tailed.
    """
    if any(baseline <= 0 or router <= 0 for baseline, router in pairs):
        raise ValueError("billed cost must be positive for every paired run")
    log_ratios = [math.log(router / baseline) for baseline, router in pairs]
    generator = random.Random(seed)
    draws = sorted(
        statistics.fmean(log_ratios[generator.randrange(len(log_ratios))] for _ in log_ratios)
        for _ in range(resamples)
    )
    return (
        math.exp(statistics.fmean(log_ratios)),
        math.exp(draws[int(0.025 * resamples)]),
        math.exp(draws[int(0.975 * resamples) - 1]),
    )


def tasks_for_cost_bound(
    tasks: int, ratio: float, ratio_high: float, cost_limit: float
) -> int | None:
    """Tasks needed for the CI's upper bound to reach ``cost_limit``, scaling as 1/sqrt(n)."""
    headroom = math.log(cost_limit) - math.log(ratio)
    half_width = math.log(ratio_high) - math.log(ratio)
    if headroom <= 0:
        return None
    return max(tasks, math.ceil(tasks * (half_width / headroom) ** 2 - 1e-9))


def cusum_alarm(
    overrides: Iterable[bool],
    baseline_rate: float,
    shift: float = OVERRIDE_GUARDRAIL,
    threshold: float = 4.0,
) -> int | None:
    """Index of the first Bernoulli CUSUM alarm for an override-rate rise of ``shift``."""
    if not 0 < baseline_rate < 1 - shift:
        raise ValueError("baseline rate must leave room for the shift")
    elevated = baseline_rate + shift
    on_override = math.log(elevated / baseline_rate)
    on_clean = math.log((1 - elevated) / (1 - baseline_rate))
    score = 0.0
    for index, overridden in enumerate(overrides):
        score = max(0.0, score + (on_override if overridden else on_clean))
        if score > threshold:
            return index
    return None


# ---------------------------------------------------------------- verdicts


@dataclass
class OfflineVerdict:
    verdict: str
    pairs: int
    discordant_router_only: int
    discordant_baseline_only: int
    success_delta: float
    success_low: float
    success_high: float
    cost_ratio: float
    cost_ratio_low: float
    cost_ratio_high: float
    total_cost_ratio: float
    max_task_cost_ratio: float
    share_tasks_over_2x: float
    binding: str | None = None
    tasks_needed: int | None = None
    unscored_baseline: int = 0
    unscored_router: int = 0


@dataclass(frozen=True)
class CostRoute:
    """A success condition with its geomean-CI limit and total-$ guard."""

    name: str
    geomean_limit: float
    total_limit: float


SUCCESS_ROUTE = CostRoute("success", 1 + MAX_COST_INCREASE, 1 + MAX_COST_INCREASE)
CHEAPER_ROUTE = CostRoute("cheaper", 1 - MIN_COST_SAVING, 1.0)


def offline_verdict(replay: Sequence[dict[str, Any]]) -> OfflineVerdict:
    """Apply the promotion bar to paired replay results.

    Each replay row is ``{task_id, arm: baseline|router, passed, usd}``. A route
    promotes when the geomean cost CI's upper bound meets its limit and the total-$
    ratio point estimate meets its guard.
    """
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for result in replay:
        by_task.setdefault(str(result["task_id"]), {})[result["arm"]] = result
    complete = [arms for arms in by_task.values() if {"baseline", "router"} <= arms.keys()]
    # A pair with an unanswered arm has nothing to compare; the counts keep the loss visible.
    paired = [
        arms for arms in complete if all(arm.get("passed") is not None for arm in arms.values())
    ]
    if not paired:
        raise ValueError("no task has a scored baseline and router result")
    both = only_router = only_baseline = neither = 0
    for arms in paired:
        router_passed = bool(arms["router"]["passed"])
        baseline_passed = bool(arms["baseline"]["passed"])
        both += router_passed and baseline_passed
        only_router += router_passed and not baseline_passed
        only_baseline += baseline_passed and not router_passed
        neither += not (router_passed or baseline_passed)
    delta, low, high = paired_difference_interval(both, only_router, only_baseline, neither)
    cost_pairs = [
        (float(arms["baseline"]["usd"]), float(arms["router"]["usd"])) for arms in paired
    ]
    ratio, ratio_low, ratio_high = bootstrap_cost_ratio(cost_pairs)
    total_ratio = sum(router for _, router in cost_pairs) / sum(base for base, _ in cost_pairs)
    task_ratios = [router / baseline for baseline, router in cost_pairs]

    routes = []
    if low > 0:
        routes.append(SUCCESS_ROUTE)
    if low >= -NON_INFERIORITY_MARGIN:
        routes.append(CHEAPER_ROUTE)
    binding = tasks_needed = None
    if len(paired) < MIN_PAIRED_TASKS:
        verdict = "insufficient"
    elif any(
        ratio_high <= route.geomean_limit and total_ratio <= route.total_limit for route in routes
    ):
        verdict = "promote"
    elif high < -NON_INFERIORITY_MARGIN:
        verdict = "reject"
    else:
        verdict = "not_separated"
        more_tasks_help = [
            route
            for route in routes
            if ratio < route.geomean_limit and total_ratio <= route.total_limit
        ]
        if more_tasks_help:
            binding = "cost"
            tasks_needed = tasks_for_cost_bound(
                len(paired), ratio, ratio_high, more_tasks_help[0].geomean_limit
            )
        elif any(ratio < route.geomean_limit for route in routes):
            binding = "total_cost"
    return OfflineVerdict(
        verdict=verdict,
        pairs=len(paired),
        discordant_router_only=only_router,
        discordant_baseline_only=only_baseline,
        success_delta=delta,
        success_low=low,
        success_high=high,
        cost_ratio=ratio,
        cost_ratio_low=ratio_low,
        cost_ratio_high=ratio_high,
        total_cost_ratio=total_ratio,
        max_task_cost_ratio=max(task_ratios),
        share_tasks_over_2x=sum(task > 2 for task in task_ratios) / len(task_ratios),
        binding=binding,
        tasks_needed=tasks_needed,
        unscored_baseline=sum(arms["baseline"].get("passed") is None for arms in complete),
        unscored_router=sum(arms["router"].get("passed") is None for arms in complete),
    )


def canary_guardrail(rows: Sequence[DecisionRow]) -> dict[str, Any]:
    """Override-rate delta of canary vs baseline decisions, excluded decisions dropped."""
    eligible = [row for row in rows if row.exclusion is None and not row.pinned]
    canary = [row for row in eligible if row.arm == "canary"]
    baseline = [row for row in eligible if row.arm == "baseline"]
    report: dict[str, Any] = {"canary": len(canary), "baseline": len(baseline)}
    if not canary or not baseline:
        return {**report, "verdict": "insufficient"}
    delta, low, high = independent_difference_interval(
        sum(row.overridden for row in canary),
        len(canary),
        sum(row.overridden for row in baseline),
        len(baseline),
    )
    if len(canary) < MIN_CANARY_DECISIONS:
        verdict = "insufficient"
    else:
        verdict = "pass" if high <= OVERRIDE_GUARDRAIL else "fail"
    return {**report, "override_delta": delta, "low": low, "high": high, "verdict": verdict}


# ---------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("dataset", "estimate", "canary"):
        command = commands.add_parser(name)
        command.add_argument("--decisions", type=Path, required=True)
        command.add_argument("--outcomes", type=Path)
        if name == "estimate":
            command.add_argument("--router-backend", default="smart")
            command.add_argument("--tasks", type=int, default=MIN_PAIRED_TASKS)
    commands.add_parser("verdict").add_argument("--replay", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "verdict":
        result: Any = asdict(offline_verdict(read_jsonl(args.replay)))
    else:
        rows = build_dataset(
            read_jsonl(args.decisions), read_jsonl(args.outcomes) if args.outcomes else ()
        )
        if args.command == "dataset":
            result = coverage_by_site(rows)
        elif args.command == "estimate":
            result = estimate_offline_cost(rows, args.router_backend, args.tasks)
        else:
            result = canary_guardrail(rows)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

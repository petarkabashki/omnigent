from __future__ import annotations

import json
import math
import random

import pytest

from dev.benchmarks.routing.eval import (
    DecisionRow,
    build_dataset,
    canary_guardrail,
    coverage_by_site,
    cusum_alarm,
    estimate_offline_cost,
    main,
    offline_verdict,
    paired_difference_interval,
    tasks_for_cost_bound,
    wilson_interval,
)


def _decision(decision_id: str, backend: str = "rules", **fields) -> dict:
    return {
        "decision_id": decision_id,
        "session_id": f"s-{decision_id}",
        "backend": backend,
        "site": "session_create",
        "decision": {"model": "big"},
        "actual": {"model": "big"},
        **fields,
    }


def _labels(outcomes: list[dict], **decision_fields) -> DecisionRow:
    (row,) = build_dataset([_decision("d1", **decision_fields)], outcomes)
    return row


def test_backends_for_one_request_share_a_row() -> None:
    rows = build_dataset([_decision("d1", "rules"), _decision("d1", "smart")])
    assert len(rows) == 1
    assert set(rows[0].proposals) == {"rules", "smart"}


def test_legacy_records_without_decision_id_group_by_session() -> None:
    legacy = [
        {"session_id": "s1", "backend": backend, "decision": {}, "actual": {}}
        for backend in ("rules", "smart")
    ]
    (row,) = build_dataset(legacy)
    assert row.decision_id == "session:s1"
    assert row.site == "session_create"


@pytest.mark.parametrize(
    ("outcomes", "overridden", "success"),
    [
        ([], False, None),
        ([{"event": "check", "passed": True}], False, True),
        ([{"event": "check", "passed": False}], False, False),
        ([{"event": "check", "passed": False}, {"event": "accept"}], False, True),
        ([{"event": "accept"}], False, True),
        ([{"event": "override", "kind": "interrupt", "actor": "owner"}], True, None),
        (
            [
                {"event": "override", "kind": "reset", "actor": "owner"},
                {"event": "check", "passed": True},
            ],
            True,
            False,
        ),
        (
            [
                {"event": "override", "kind": "interrupt", "actor": "agent"},
                {"event": "check", "passed": True},
            ],
            False,
            True,
        ),
        ([{"event": "override", "kind": "retitle", "actor": "owner"}], False, None),
    ],
)
def test_labels_follow_owner_definitions(outcomes, overridden, success) -> None:
    row = _labels([{"decision_id": "d1", **event} for event in outcomes])
    assert (row.overridden, row.success) == (overridden, success)


def test_latest_check_wins_and_costs_sum_with_retries() -> None:
    row = _labels(
        [
            {"decision_id": "d1", "event": "check", "passed": False, "at": "2026-11-03T10:00"},
            {"decision_id": "d1", "event": "check", "passed": True, "at": "2026-11-03T11:00"},
            {"decision_id": "d1", "event": "cost", "usd_billed": 0.5},
            {"decision_id": "d1", "event": "cost", "usd_billed": 0.25, "retries": 1},
        ]
    )
    assert row.success is True
    assert row.cost_usd == pytest.approx(0.75)


def test_coverage_and_eligibility() -> None:
    complete = {"check_ref": "pytest x", "input_ref": "sha256:a", "git_commit": "abc"}
    rows = build_dataset(
        [
            _decision("eligible", **complete),
            _decision("pinned", pinned={"model": True, "host": False}, **complete),
            _decision("excluded", exclusion="role", **complete),
            _decision("no-check", input_ref="sha256:b", git_commit="abc"),
        ]
    )
    site = coverage_by_site(rows)["session_create"]
    assert site["decisions"] == 4
    assert site["offline_eligible"] == 1
    assert site["check_coverage"] == pytest.approx(0.75)
    assert next(row for row in rows if row.decision_id == "pinned").pinned == ["model"]


def test_coverage_counts_dropped_oversized_records() -> None:
    stub = {
        "decision_id": "big",
        "session_id": "s-big",
        "backend": "rules",
        "truncated": "dropped",
    }
    rows = build_dataset([stub, _decision("d1")])
    assert coverage_by_site(rows)["session_create"]["dropped"] == 1


def test_estimate_prices_router_arm_by_observed_model_cost() -> None:
    decisions = [
        _decision("a", decision={"model": "big"}, actual={"model": "big"}),
        _decision("a", "smart", decision={"model": "small"}, actual={"model": "big"}),
        _decision("b", decision={"model": "small"}, actual={"model": "small"}),
        _decision("b", "smart", decision={"model": "small"}, actual={"model": "small"}),
    ]
    outcomes = [
        {"decision_id": "a", "event": "cost", "usd_billed": 1.0},
        {"decision_id": "b", "event": "cost", "usd_billed": 0.2},
    ]
    site = estimate_offline_cost(build_dataset(decisions, outcomes), "smart")["session_create"]
    assert site["router_median_usd"] == pytest.approx(0.2)
    assert site["estimate_usd"] == pytest.approx(300 * (0.6 + 0.2) * 1.25)
    assert site["router_unpriced_fraction"] == 0


def test_wilson_interval_bounds() -> None:
    low, high = wilson_interval(0, 10)
    assert low == 0.0 and 0.25 < high < 0.35
    assert wilson_interval(5, 0) == (0.0, 1.0)


def test_paired_interval_is_antisymmetric_and_contains_estimate() -> None:
    delta, low, high = paired_difference_interval(120, 40, 25, 115)
    assert low < delta < high
    swapped = paired_difference_interval(120, 25, 40, 115)
    assert swapped == pytest.approx((-delta, -high, -low))


def test_paired_interval_has_nominal_coverage() -> None:
    generator = random.Random(7)
    true_delta, covered, trials = 0.05, 0, 400
    for _ in range(trials):
        counts = [0, 0, 0, 0]
        for _ in range(300):
            baseline = generator.random() < 0.6
            router = baseline if generator.random() < 0.8 else generator.random() < 0.85
            index = 0 if router and baseline else 1 if router else 2 if baseline else 3
            counts[index] += 1
        _, low, high = paired_difference_interval(*counts)
        covered += low <= true_delta <= high
    assert 0.92 <= covered / trials <= 0.98


def _replay(
    tasks: int, router_pass: float, baseline_pass: float, router_usd: float, seed: int = 1
):
    generator = random.Random(seed)
    rows = []
    for task in range(tasks):
        shared = generator.random()
        rows.append(
            {
                "task_id": task,
                "arm": "baseline",
                "passed": shared < baseline_pass,
                "usd": 1.0 + generator.random() * 0.1,
            }
        )
        rows.append(
            {
                "task_id": task,
                "arm": "router",
                "passed": shared < router_pass,
                "usd": router_usd + generator.random() * 0.1,
            }
        )
    return rows


@pytest.mark.parametrize(
    ("tasks", "router_pass", "router_usd", "verdict"),
    [
        (300, 0.80, 1.0, "promote"),
        (300, 0.70, 0.70, "promote"),
        (300, 0.70, 1.0, "not_separated"),
        (300, 0.55, 1.0, "reject"),
        (299, 0.80, 0.5, "insufficient"),
    ],
)
def test_offline_verdict_applies_promotion_bar(tasks, router_pass, router_usd, verdict) -> None:
    result = offline_verdict(_replay(tasks, router_pass, 0.70, router_usd))
    assert result.verdict == verdict
    assert result.pairs == tasks
    assert result.total_cost_ratio == pytest.approx((router_usd + 0.05) / 1.05, rel=0.05)


def test_offline_verdict_ignores_unpaired_tasks() -> None:
    rows = [
        *_replay(300, 0.8, 0.7, 1.0),
        {"task_id": "x", "arm": "router", "passed": True, "usd": 1},
    ]
    assert offline_verdict(rows).pairs == 300


def _noisy_cost_replay(tasks: int, seed: int) -> list[dict]:
    generator = random.Random(seed)
    rows = []
    for task in range(tasks):
        baseline_usd = math.exp(generator.gauss(0, 0.2))
        router_usd = baseline_usd * math.exp(generator.gauss(math.log(0.82), 0.6))
        rows.append({"task_id": task, "arm": "baseline", "passed": True, "usd": baseline_usd})
        rows.append({"task_id": task, "arm": "router", "passed": True, "usd": router_usd})
    return rows


def test_cost_binding_reports_tasks_needed_and_that_n_suffices() -> None:
    result = offline_verdict(_noisy_cost_replay(300, seed=5))
    assert result.verdict == "not_separated"
    assert result.cost_ratio < 0.85 < result.cost_ratio_high
    assert result.total_cost_ratio <= 1.0
    assert result.binding == "cost"
    assert result.tasks_needed is not None and result.tasks_needed > 300
    larger = offline_verdict(_noisy_cost_replay(2 * result.tasks_needed, seed=6))
    assert larger.verdict == "promote"


def _blowup_replay(tasks: int) -> list[dict]:
    rows = []
    for task in range(tasks):
        router_usd = 20.0 if task % 50 == 0 else 0.6 + (task % 7) * 0.01
        rows.append({"task_id": task, "arm": "baseline", "passed": True, "usd": 1.0})
        rows.append({"task_id": task, "arm": "router", "passed": True, "usd": router_usd})
    return rows


def test_total_cost_guard_blocks_occasional_blowups() -> None:
    result = offline_verdict(_blowup_replay(300))
    assert result.cost_ratio_high < 0.85
    assert result.total_cost_ratio > 1.0
    assert result.verdict == "not_separated"
    assert result.binding == "total_cost"
    assert result.tasks_needed is None
    assert result.max_task_cost_ratio == pytest.approx(20.0)
    assert result.share_tasks_over_2x == pytest.approx(6 / 300)


def test_tasks_for_cost_bound() -> None:
    assert tasks_for_cost_bound(300, 0.8, 0.8 * math.exp(0.1), 0.8 * math.exp(0.05)) == 1200
    assert tasks_for_cost_bound(300, 0.9, 0.95, 0.85) is None


def test_cost_ratio_rejects_non_positive_cost() -> None:
    rows = _replay(300, 0.8, 0.7, 1.0)
    rows[1]["usd"] = 0
    with pytest.raises(ValueError):
        offline_verdict(rows)


def _arm_rows(arm: str, total: int, overridden: int, **fields) -> list[DecisionRow]:
    rows = build_dataset([_decision(f"{arm}{i}", arm=arm, **fields) for i in range(total)])
    for row in rows[:overridden]:
        row.overridden = True
    return rows


@pytest.mark.parametrize(
    ("canary_total", "canary_overridden", "verdict"),
    [(400, 40, "pass"), (400, 80, "fail"), (99, 5, "insufficient")],
)
def test_canary_guardrail(canary_total, canary_overridden, verdict) -> None:
    rows = _arm_rows("baseline", 4000, 400) + _arm_rows("canary", canary_total, canary_overridden)
    rows += _arm_rows("canary", 50, 50, exclusion="critical")
    report = canary_guardrail(rows)
    assert report["verdict"] == verdict
    assert report["canary"] == canary_total


def test_cusum_alarms_on_shift_but_not_on_baseline() -> None:
    generator = random.Random(3)
    steady = [generator.random() < 0.10 for _ in range(500)]
    shifted = [generator.random() < 0.25 for _ in range(500)]
    assert cusum_alarm(steady, 0.10) is None
    alarm = cusum_alarm(steady + shifted, 0.10)
    assert alarm is not None and alarm >= 500
    with pytest.raises(ValueError):
        cusum_alarm([], 0.97)


def test_cli_dataset(tmp_path, capsys) -> None:
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("\n".join(json.dumps(_decision(f"d{i}")) for i in range(3)) + "\n")
    assert main(["dataset", "--decisions", str(decisions)]) == 0
    assert json.loads(capsys.readouterr().out)["session_create"]["decisions"] == 3

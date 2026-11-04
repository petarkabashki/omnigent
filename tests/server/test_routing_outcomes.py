"""Routing outcome stream: append path, emitters, readers and the usage hook."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from omnigent.server import routing_outcomes as ro
from omnigent.server import shadow_routing as sr
from omnigent.server.routes._sessions.orchestration import (
    _accumulate_session_usage,
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_AGENT_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    return tmp_path


def _log_decision(session_id: str, decision_id: str) -> None:
    sr.append_decision(
        {"decision_id": decision_id, "session_id": session_id, "backend": "rules", "decision": {}}
    )


def test_stream_sits_beside_decisions_with_0600(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    assert ro.record_accept("s1", "owner")
    assert ro.outcomes_path() == data_dir / "routing" / "outcomes.jsonl"
    assert stat.S_IMODE(os.stat(ro.outcomes_path()).st_mode) == 0o600


def test_session_without_decision_emits_nothing(data_dir: Path) -> None:
    assert not ro.record_accept("unrouted", "owner")
    _log_decision("s1", "d1")
    assert not ro.record_accept("other", "owner")
    assert not ro.outcomes_path().exists()


def test_decision_logged_after_first_lookup_is_found(data_dir: Path) -> None:
    assert ro.decision_id_for_session("s1") is None
    _log_decision("s1", "d1")
    assert ro.decision_id_for_session("s1") == "d1"
    _log_decision("s2", "d2")
    assert ro.decision_id_for_session("s2") == "d2"


def test_summary_takes_max_regardless_of_append_order(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    ro.record_cost("s1", {"total_cost_usd": 5.0, "policy_cost_usd": 6.0}, source="harness")
    ro.record_cost("s1", {"total_cost_usd": 3.0, "policy_cost_usd": 7.5}, source="harness")
    ro.record_ended("s1", {"total_cost_usd": 4.0}, reason="stop")
    summary = ro.summarize_outcomes()["d1"]
    assert (summary["usd_total"], summary["usd_policy"]) == (5.0, 7.5)
    assert summary["ended_at"] is not None


def test_ended_carries_usd_total_even_when_unpriced(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    ro.record_ended("s1", {}, reason="delete")
    (record,) = ro.iter_outcomes()
    assert record["event"] == "ended" and record["usd_total"] is None
    assert record["usd_policy"] is None and record["reason"] == "delete"


def test_check_status_and_passed(data_dir: Path) -> None:
    for status, passed in [("passed", True), ("failed", False), ("error", None)]:
        ro.record_check("d1", status, "sha256:abc")
        assert list(ro.iter_outcomes())[-1]["passed"] is passed
    assert ro.record_check("d1", "flaky", "sha256:abc") is False


def test_check_declared_and_latest_check_win(data_dir: Path) -> None:
    ro.append_outcome("d1", "check_declared", {"check_ref": "sha256:old", "at": 1.0})
    ro.append_outcome("d1", "check_declared", {"check_ref": "sha256:new", "at": 2.0})
    ro.append_outcome("d1", "check", {"status": "failed", "passed": False, "at": 3.0})
    ro.append_outcome("d1", "check", {"status": "passed", "passed": True, "at": 2.5})
    summary = ro.summarize_outcomes()["d1"]
    assert summary["check_declared"]["check_ref"] == "sha256:new"
    assert summary["check"]["status"] == "failed"


def test_override_records_actor_and_rejects_unknown_kind(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    assert ro.record_override("s1", "interrupt", "owner")
    assert ro.record_override("s1", "reset", "collaborator")
    kinds = [(o["kind"], o["actor"]) for o in ro.summarize_outcomes()["d1"]["overrides"]]
    assert kinds == [("interrupt", "owner"), ("reset", "collaborator")]
    with pytest.raises(ValueError):
        ro.record_override("s1", "pause", "owner")


@pytest.mark.parametrize(
    ("actors", "excluded"),
    [
        (["unknown"], True),
        (["unknown", "harness"], True),
        (["unknown", "owner"], False),
        (["agent"], False),
        (["collaborator"], False),
    ],
)
def test_unknown_actor_excludes_the_step_unless_the_owner_settled_it(
    data_dir: Path, actors: list[str], excluded: bool
) -> None:
    _log_decision("s1", "d1")
    for actor in actors:
        ro.record_override("s1", "model_change", actor)
    assert ro.summarize_outcomes()["d1"]["success_excluded"] is excluded


def test_corrupt_lines_are_skipped(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    ro.record_accept("s1", "owner")
    with ro.outcomes_path().open("a") as handle:
        handle.write("not json\n\n[1]\n")
    assert list(ro.summarize_outcomes()) == ["d1"]


def test_cost_stream_matches_session_usage_across_a_reset(
    data_dir: Path, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")

    def report(cost: float, epoch: int) -> None:
        _persist_native_cumulative_usage(
            conv.id,
            {"cumulative_cost_usd": cost, "policy_cost_usd": cost + 1, "context_epoch": epoch},
            store,
        )

    report(3.0, 0)
    assert store.advance_context_epoch(conv.id, 1)
    report(1.0, 1)
    report(2.0, 1)
    # A report from before the reset is dropped and must not reach the stream.
    report(9.0, 0)

    stored = store.get_conversation(conv.id).session_usage  # type: ignore[union-attr]
    summary = ro.summarize_outcomes()["d1"]
    assert stored["total_cost_usd"] == 5.0
    assert ro.usage_gap(summary, stored) == {"usd_total": 0.0, "usd_policy": 0.0}
    epochs = [r["context_epoch"] for r in ro.iter_outcomes() if r["event"] == "cost"]
    assert epochs == [0, 1, 1]
    last = list(ro.iter_outcomes())[-1]
    assert last["session_usage"]["context_reset_base"]["total_cost_usd"] == 3.0


def test_usage_gap_reports_under_count_and_unpriced() -> None:
    assert ro.usage_gap({"usd_total": 4.0, "usd_policy": None}, {"total_cost_usd": 5.0}) == {
        "usd_total": -1.0,
        "usd_policy": None,
    }


# ── Contract: cost, rotation, buffering, robustness ────────────────────────


def _lines(event: str) -> list[dict]:
    return [r for r in ro.iter_outcomes() if r["event"] == event]


def test_no_total_writes_no_cost_line_and_reader_survives_null_ended(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    assert not ro.record_cost("s1", {"policy_cost_usd": 1.0}, source=None)
    assert not ro.record_cost("s1", {"total_cost_usd": None}, source=None)
    ro.record_ended("s1", {"policy_cost_usd": 1.0}, reason="stop")
    assert _lines("cost") == []
    (ended,) = _lines("ended")
    assert ended["usd_total"] is None and ended["cost_source"] == "none"
    assert ro.summarize_outcomes()["d1"]["usd_total"] is None


def test_cost_source_and_native_completeness(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    ro.record_cost("s1", {"total_cost_usd": 1.0}, source="harness")
    assert _lines("cost")[-1]["cost_source"] == "harness"
    assert _lines("cost")[-1]["native_cost_complete"] is None
    ro.record_cost("s1", {"total_cost_usd": 2.0}, source="catalog")
    assert _lines("cost")[-1]["cost_source"] == "mixed"
    _log_decision("s2", "d2")
    ro.record_cost("s2", {"total_cost_usd": 1.0}, source="catalog", native=True)
    ro.record_ended(
        "s2", {"total_cost_usd": 1.0, "native_cost_complete": True}, reason="stop", native=True
    )
    assert [r["native_cost_complete"] for r in _lines("cost")][-1] is False
    assert _lines("ended")[-1]["native_cost_complete"] is True
    summary = ro.summarize_outcomes()
    assert (
        summary["d1"]["cost_source"] == "mixed" and summary["d1"]["native_cost_complete"] is None
    )


def test_rotation_keeps_decision_and_sums_sessions(data_dir: Path) -> None:
    _log_decision("old", "d1")
    _log_decision("new", "d2")  # the rotated-to session's own creation decision
    ro.record_cost("old", {"total_cost_usd": 5.0}, source="harness")
    ro.record_cost("old", {"total_cost_usd": 3.0}, source="harness")
    assert ro.record_rotation("old", "new")
    assert not ro.record_rotation("old", "new")  # repeat is a no-op
    ro.record_cost("new", {"total_cost_usd": 2.0}, source="harness")
    ro.record_ended("new", {"total_cost_usd": 2.0}, reason="stop")
    assert {r["decision_id"] for r in ro.iter_outcomes()} == {"d1"}
    assert {r["session_id"] for r in _lines("cost")} == {"old", "new"}
    (rotated,) = _lines("rotated")
    assert (rotated["from_session"], rotated["to_session"]) == ("old", "new")
    summary = ro.summarize_outcomes()["d1"]
    assert summary["usd_total"] == 7.0
    assert summary["sessions"]["old"]["usd_total"] == 5.0


def test_event_before_decision_is_buffered_then_written(data_dir: Path) -> None:
    assert not ro.record_override("s1", "interrupt", "owner", path=ro.outcomes_path())
    assert not ro.outcomes_path().exists()
    _log_decision("s1", "d1")
    ro.flush_pending_outcomes()
    assert [(r["decision_id"], r["kind"]) for r in _lines("override")] == [("d1", "interrupt")]


def test_unjoinable_event_is_counted_not_lost_silently(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ro.record_accept("ghost", "owner", path=ro.outcomes_path())
    monkeypatch.setattr(ro, "_PENDING_TTL_S", -1.0)
    ro.flush_pending_outcomes()
    assert ro.count_dropped() == {"no_decision": 1}
    assert ro.dropped_outcomes >= 1


def test_emitters_never_raise(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _log_decision("s1", "d1")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ro, "append_outcome", boom)
    assert ro.record_override("s1", "reset", "owner") is False
    assert ro.record_check("d1", "passed", "sha256:x") is False
    assert ro.record_check("d1", "bogus", "sha256:x") is False
    assert ro.record_check_declared("d1", "sha256:x") is False


def test_index_ignores_partial_line_and_resets_when_log_shrinks(data_dir: Path) -> None:
    path = sr.decisions_path()
    path.parent.mkdir(parents=True)
    whole = b'{"decision_id":"d1","session_id":"s1"}\n'
    path.write_bytes(whole + b'{"decision_id":"d2","session_')
    assert ro.decision_id_for_session("s1") == "d1"
    assert ro.decision_id_for_session("s2") is None
    path.write_bytes(whole + b'{"decision_id":"d2","session_id":"s2"}\n')
    assert ro.decision_id_for_session("s2") == "d2"
    path.write_bytes(b'{"decision_id":"d9","session_id":"s9"}\n')  # shorter: rotated away
    assert ro.decision_id_for_session("s9") == "d9"
    assert ro.decision_id_for_session("s1") is None


def test_short_write_is_finished_and_failed_write_ends_the_fragment(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = data_dir / "routing" / "x.jsonl"
    real_write = os.write
    state = {"calls": 0}

    def short_write(fd: int, data: bytes) -> int:
        state["calls"] += 1
        return real_write(fd, data[: len(data) // 2] if state["calls"] == 1 else data)

    monkeypatch.setattr(sr.os, "write", short_write)
    sr.append_jsonl_line(target, '{"a":1,"pad":"' + "x" * 40 + '"}')
    sr.append_jsonl_line(target, '{"b":2}')
    assert next(iter(next(sr.iter_jsonl_records(target)))) == "a"
    assert len(list(sr.iter_jsonl_records(target))) == 2

    def failing(fd: int, data: bytes) -> int:
        if data.startswith(b'{"c"'):
            return real_write(fd, data[:5])  # short, then the retry below fails
        if not data.startswith(b'{"d"') and data != b"\n":
            raise OSError("boom")
        return real_write(fd, data)

    monkeypatch.setattr(sr.os, "write", failing)
    with pytest.raises(OSError):
        sr.append_jsonl_line(target, '{"c":3,"pad":"yyyyyyyy"}')
    monkeypatch.setattr(sr.os, "write", real_write)
    sr.append_jsonl_line(target, '{"d":4}')
    assert [next(iter(r)) for r in sr.iter_jsonl_records(target)] == ["a", "b", "d"]


def test_reader_survives_invalid_utf8(data_dir: Path) -> None:
    target = data_dir / "routing" / "bad.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'\xff\xfe{"a":\n{"ok":1}\n')
    assert list(sr.iter_jsonl_records(target)) == [{"ok": 1}]


def test_existing_files_are_tightened_to_0600_and_0700(data_dir: Path) -> None:
    target = data_dir / "routing" / "outcomes.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("")
    target.chmod(0o644)
    target.parent.chmod(0o755)
    sr.append_jsonl_line(target, '{"a":1}')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_native_report_snapshot_carries_completeness(data_dir: Path, db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_cost_usd": 3.0, "cost_source": "harness", "cost_complete": True},
        store,
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 4.0, "cost_source": "harness"}, store
    )
    cost = _lines("cost")
    assert [(r["cost_source"], r["native_cost_complete"]) for r in cost] == [
        ("harness", True),
        ("harness", False),
    ]


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ({"cost_source": "harness"}, "harness"),
        ({"cost_source": "catalog"}, "catalog"),
        ({}, "unknown"),
        ({"cost_source": "billed"}, "unknown"),
        ({"cost_source": ["harness"]}, "unknown"),
    ],
)
def test_native_cost_is_labelled_by_its_reporter_never_by_default(
    data_dir: Path, db_uri: str, declared: dict[str, object], expected: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 50.0, **declared}, store)
    (cost,) = _lines("cost")
    assert (cost["usd_total"], cost["cost_source"]) == (50.0, expected)
    assert ro.summarize_outcomes()["d1"]["cost_source"] == expected


def test_token_priced_native_report_stays_catalog_whatever_it_declares(
    data_dir: Path, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.llms import context_window

    monkeypatch.setattr(
        context_window, "fetch_model_pricing_with_provider", lambda *a, **k: {"priced": True}
    )
    monkeypatch.setattr(context_window, "compute_llm_cost", lambda *a, **k: 0.25)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 2, "model": "priced", "cost_source": "harness"},
        store,
    )
    assert [r["cost_source"] for r in _lines("cost")] == ["catalog"]


def test_credits_never_reach_a_dollar_field(data_dir: Path, db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    report = {"cumulative_cost_usd": 50.0, "cost_source": "credits"}
    _persist_native_cumulative_usage(conv.id, report, store)
    # A later report and a process restart must not turn the total back into dollars.
    ro._sources_seen.clear()
    _persist_native_cumulative_usage(conv.id, {"policy_cost_usd": 60.0}, store)
    usage = store.get_conversation(conv.id).session_usage
    ro.record_ended(conv.id, usage, reason="stop", native=True)
    records = [*_lines("cost"), *_lines("ended")]
    assert len(records) == 3
    for record in records:
        assert (record["usd_total"], record["usd_policy"]) == (None, None)
        assert (record["credits_total"], record["cost_source"]) == (50.0, "credits")
        assert not {"total_cost_usd", "policy_cost_usd"} & set(record["session_usage"])
    summary = ro.summarize_outcomes()["d1"]
    assert (summary["usd_total"], summary["usd_policy"]) == (None, None)
    assert (summary["credits_total"], summary["cost_source"]) == (50.0, "credits")


def _relay_response(**usage: object) -> dict[str, object]:
    return {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, **usage}}


def test_relay_harness_reported_cost_is_streamed_as_harness(data_dir: Path, db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _accumulate_session_usage(_relay_response(model="m", cost_usd=1.5), conv.id, store)
    _accumulate_session_usage(_relay_response(model="m", cost_usd=0.5), conv.id, store)
    cost = _lines("cost")
    assert [(r["usd_total"], r["cost_source"], r["session_id"]) for r in cost] == [
        (1.5, "harness", conv.id),
        (2.0, "harness", conv.id),
    ]


def test_relay_catalog_priced_cost_is_streamed_as_catalog(
    data_dir: Path, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.llms import context_window

    monkeypatch.setattr(
        context_window, "fetch_model_pricing_with_provider", lambda *a, **k: {"priced": True}
    )
    monkeypatch.setattr(context_window, "compute_llm_cost", lambda *a, **k: 0.25)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _accumulate_session_usage(_relay_response(model="m"), conv.id, store)
    (cost,) = _lines("cost")
    assert (cost["usd_total"], cost["cost_source"]) == (0.25, "catalog")


def test_relay_unpriced_turn_writes_no_cost_line(
    data_dir: Path, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.llms import context_window

    monkeypatch.setattr(context_window, "fetch_model_pricing_with_provider", lambda *a, **k: None)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    _accumulate_session_usage(_relay_response(model="m"), conv.id, store)
    assert _lines("cost") == []


async def test_failed_launch_closes_the_outcome_with_its_reason(
    data_dir: Path, db_uri: str
) -> None:
    from omnigent.server.startup_inputs import ManagedStartupInputs

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="routed", agent_id=_AGENT_ID)
    _log_decision(conv.id, "d1")
    await ManagedStartupInputs(store, hosts=None).record_launch_failure(conv.id, "no capacity")  # type: ignore[arg-type]
    (ended,) = _lines("ended")
    assert (ended["reason"], ended["usd_total"], ended["session_id"]) == (
        "launch_failed",
        None,
        conv.id,
    )

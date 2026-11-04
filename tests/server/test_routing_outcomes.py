"""Routing outcome stream: append path, emitters, readers and the usage hook."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from omnigent.server import routing_outcomes as ro
from omnigent.server import shadow_routing as sr
from omnigent.server.routes._sessions.orchestration import _persist_native_cumulative_usage
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
    ro.record_cost("s1", {"total_cost_usd": 5.0, "policy_cost_usd": 6.0})
    ro.record_cost("s1", {"total_cost_usd": 3.0, "policy_cost_usd": 7.5})
    ro.record_ended("s1", {"total_cost_usd": 4.0}, reason="stop")
    summary = ro.summarize_outcomes()["d1"]
    assert (summary["usd_billed"], summary["usd_policy"]) == (5.0, 7.5)
    assert summary["ended_at"] is not None


def test_ended_carries_usd_billed_even_when_unpriced(data_dir: Path) -> None:
    _log_decision("s1", "d1")
    ro.record_ended("s1", {}, reason="delete")
    (record,) = ro.iter_outcomes()
    assert record["event"] == "ended" and record["usd_billed"] is None
    assert record["usd_policy"] is None and record["reason"] == "delete"


def test_check_status_and_passed(data_dir: Path) -> None:
    for status, passed in [("passed", True), ("failed", False), ("error", None)]:
        ro.record_check("d1", status, "sha256:abc")
        assert list(ro.iter_outcomes())[-1]["passed"] is passed
    with pytest.raises(ValueError):
        ro.record_check("d1", "flaky", "sha256:abc")


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
    assert ro.usage_gap(summary, stored) == {"usd_billed": 0.0, "usd_policy": 0.0}
    epochs = [r["context_epoch"] for r in ro.iter_outcomes() if r["event"] == "cost"]
    assert epochs == [0, 1, 1]
    last = list(ro.iter_outcomes())[-1]
    assert last["session_usage"]["context_reset_base"]["total_cost_usd"] == 3.0


def test_usage_gap_reports_under_count_and_unpriced() -> None:
    assert ro.usage_gap({"usd_billed": 4.0, "usd_policy": None}, {"total_cost_usd": 5.0}) == {
        "usd_billed": -1.0,
        "usd_policy": None,
    }

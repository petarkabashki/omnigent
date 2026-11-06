"""Session cost across in-place engine switches (native, relay, per-engine rows)."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.engine_epochs import engine_epochs_from_switches
from omnigent.server.routes._sessions.orchestration import (
    _accumulate_session_usage,
    _build_session_response,
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store import EngineBinding
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_AGENT_ID = "0123456789abcdef0123456789abcdef"


def _switch(store: SqlAlchemyConversationStore, conv_id: str, harness: str, model: str) -> None:
    """Switch like the reset route: record usage_before, then advance the epoch."""
    conv = store.get_conversation(conv_id)
    assert conv is not None
    store.apply_engine_switch(
        conv_id,
        target=EngineBinding(_AGENT_ID, None, None, {}),
        expected_external_session_id=conv.external_session_id,
        response_id=f"reset_{conv.context_epoch + 1}",
        summary="handoff",
        engine_change={
            "from": {"harness": conv.session_usage.get("_h"), "model": None},
            "to": {"harness": harness, "model": model},
            "usage_before": dict(conv.session_usage),
        },
    )


def _report(
    store: SqlAlchemyConversationStore, conv_id: str, cost: float, epoch: int, **extra: Any
) -> float:
    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": cost, "model": "m", "context_epoch": epoch, **extra},
        store,
    )
    conv = store.get_conversation(conv_id)
    assert conv is not None
    return conv.session_usage["total_cost_usd"]


def test_native_total_keeps_rising_across_repeated_switches(db_uri: str) -> None:
    """A new engine's cumulative restarts near zero; the clamp must not swallow it."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=_AGENT_ID)
    assert _report(store, conv.id, 10.0, 0) == 10.0

    _switch(store, conv.id, "codex-native", "gpt")
    assert _report(store, conv.id, 0.5, 1) == 10.5
    assert _report(store, conv.id, 3.0, 1) == 13.0

    _switch(store, conv.id, "claude-native", "opus")
    assert _report(store, conv.id, 1.0, 2) == 14.0
    # A late report from the previous engine's epoch changes nothing.
    assert _report(store, conv.id, 99.0, 1) == 14.0


def test_relay_total_continues_after_a_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=_AGENT_ID)
    assert _report(store, conv.id, 10.0, 0) == 10.0
    _switch(store, conv.id, "omnigent", "m")
    _accumulate_session_usage(
        {
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "model": "m",
                "cost_usd": 2.0,
            }
        },
        conv.id,
        store,
    )
    updated = store.get_conversation(conv.id)
    assert updated is not None and updated.session_usage["total_cost_usd"] == 12.0


def test_pi_resume_baseline_excludes_the_reset_base(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi reports are added to the base, so its baseline must not include the base."""
    monkeypatch.setattr(orchestration, "_resolve_harness", lambda *a, **kw: "pi-native")
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=_AGENT_ID)
    _report(store, conv.id, 10.0, 0, cumulative_input_tokens=100, cost_complete=True)
    _switch(store, conv.id, "pi-native", "m")
    _report(store, conv.id, 2.0, 1, cumulative_input_tokens=30, cost_complete=True)
    current = store.get_conversation(conv.id)
    assert current is not None
    response = _build_session_response(current, [], "idle", agent_name="pi")
    baseline = response.native_usage_baseline
    assert baseline is not None
    assert baseline.cumulative_cost_usd == 2.0
    assert baseline.cumulative_input_tokens == 30


def test_epoch_rows_split_spend_at_each_switch() -> None:
    usage_a = {"total_cost_usd": 10.0, "input_tokens": 100, "output_tokens": 20}
    usage_b = {"total_cost_usd": 13.0, "input_tokens": 180, "output_tokens": 50}
    switches = [
        {"from": {"harness": "claude-native", "model": "opus"},
         "to": {"harness": "codex-native", "model": "gpt"}, "usage_before": usage_a},
        {"from": {"harness": "codex-native", "model": "gpt"},
         "to": {"harness": "claude-native", "model": "opus"}, "usage_before": usage_b},
    ]  # fmt: skip
    rows = engine_epochs_from_switches(
        switches, {"total_cost_usd": 14.0, "input_tokens": 200, "output_tokens": 60}
    )
    assert rows is not None
    assert [(r.epoch, r.harness, r.cost_usd, r.input_tokens, r.output_tokens) for r in rows] == [
        (0, "claude-native", 10.0, 100, 20),
        (1, "codex-native", 3.0, 80, 30),
        (2, "claude-native", 1.0, 20, 10),
    ]
    assert sum(r.cost_usd or 0 for r in rows) == 14.0
    assert engine_epochs_from_switches([], usage_a) is None

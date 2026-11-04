"""Per-model cost/token attribution for the native cumulative usage path.

``_persist_native_cumulative_usage`` receives *cumulative* session totals from
native harnesses (claude-native / codex-native). It must attribute each
report's growth to the currently-active model so per-model buckets hold each
model's own spend and sum to the flat session total across mid-session model
switches — rather than SETting every bucket to the whole running total (which
double-counted the shared baseline once the active model changed).
"""

from __future__ import annotations

import json

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.context_window import ModelPricing
from omnigent.server.routes._sessions.orchestration import (
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"


def _usage(store: SqlAlchemyConversationStore, conv_id: str) -> dict:
    conv = store.get_conversation(conv_id)
    assert conv is not None
    return dict(conv.session_usage)


@pytest.mark.parametrize("coverage", [{"cost_complete": False}, {}])
@pytest.mark.parametrize("catalog_price", [None, 0.01])
def test_pi_incomplete_cost_does_not_become_a_resume_baseline(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, coverage: dict, catalog_price: float | None
) -> None:
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing_with_provider",
        lambda *a, **kw: (
            ModelPricing(input_per_token=catalog_price, output_per_token=0)
            if catalog_price is not None
            else None
        ),
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="pi-cost", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_cost_usd": 0.75,
            "cost_complete": True,
            "model": "unpriced-model",
        },
        store,
    )
    assert _usage(store, conv.id)["native_cost_complete"] is True
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, **coverage, "model": "unpriced-model"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == (2.0 if catalog_price is not None else 0.75)
    assert usage["input_tokens"] == 200
    assert usage.get("native_cost_complete") is not True
    with pytest.raises(OmnigentError, match="cost_complete must be a bool"):
        _persist_native_cumulative_usage(
            conv.id, {"cumulative_input_tokens": 300, "cost_complete": "true"}, store
        )


def test_per_model_cost_sums_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch", agent_id=_AGENT_ID)

    # Cumulative session cost climbs on opus, then keeps climbing after a
    # /model switch to sonnet (native /cost is a running session total).
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 3.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.80, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 11.91, "model": "claude-sonnet-4-5"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # Each model holds only its own spend (opus's growth 0->10.80, sonnet's
    # 10.80->11.91), and the buckets sum to the flat total — the bug fixed here.
    assert usage["total_cost_usd"] == pytest.approx(11.91)
    assert by_model["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(10.80)
    assert by_model["claude-sonnet-4-5"]["total_cost_usd"] == pytest.approx(1.11)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(11.91)


def test_per_model_cost_accumulates_when_switching_back(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-back", agent_id=_AGENT_ID)

    # A -> B -> A: model A's bucket must accumulate across its two separate
    # active spans (not be overwritten on the second visit), so the returning
    # model's growth adds onto its earlier spend.
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.0, "model": "model-a"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 12.50, "model": "model-b"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 15.0, "model": "model-a"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # A = 10.0 (first span) + 2.50 (15.0 - 12.50 on return); B = 12.50 - 10.0.
    assert usage["total_cost_usd"] == pytest.approx(15.0)
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(12.50)
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(2.50)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(15.0)


def test_single_model_bucket_equals_flat_total(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="single", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 2.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 5.5, "model": "claude-opus-4-8"}, store
    )

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(5.5)
    assert usage["by_model"]["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(5.5)


def test_lowered_report_does_not_claw_back_bucket(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="rebased", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)
    # A forged / rebased lower report: the flat total is monotonic-clamped, and
    # the per-model delta is clamped >= 0, so neither is clawed back.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 2.0, "model": "m1"}, store)

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(10.0)
    assert usage["by_model"]["m1"]["total_cost_usd"] == pytest.approx(10.0)


def test_per_model_tokens_accumulate_from_cumulative(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="tokens", agent_id=_AGENT_ID)

    # codex-native style: cumulative token totals plus an explicit cost.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 1.0,
            "model": "gpt-5.6",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 50,
            "cumulative_cost_usd": 3.0,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    bucket = usage["by_model"]["gpt-5.6"]
    # The bucket accumulates deltas to match the flat cumulative session totals.
    assert bucket["input_tokens"] == usage["input_tokens"] == 300
    assert bucket["output_tokens"] == usage["output_tokens"] == 50
    assert bucket["total_tokens"] == usage["total_tokens"] == 350
    assert bucket["total_cost_usd"] == pytest.approx(3.0)


def test_per_model_tokens_sum_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-tokens", agent_id=_AGENT_ID)

    # codex-style: cumulative tokens (with explicit cost to avoid the catalog)
    # climb on opus, then keep climbing after a /model switch to sonnet.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 3.0,
            "model": "claude-opus-4-8",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 60,
            "cumulative_cost_usd": 11.91,
            "model": "claude-sonnet-4-5",
        },
        store,
    )

    usage = _usage(store, conv.id)
    opus = usage["by_model"]["claude-opus-4-8"]
    sonnet = usage["by_model"]["claude-sonnet-4-5"]
    # Each model holds only its own token growth, and the per-model token +
    # cost buckets sum to the flat session totals across the switch.
    assert opus["input_tokens"] == 100
    assert sonnet["input_tokens"] == 200
    assert opus["input_tokens"] + sonnet["input_tokens"] == usage["input_tokens"] == 300
    assert opus["output_tokens"] + sonnet["output_tokens"] == usage["output_tokens"] == 60
    assert opus["total_tokens"] + sonnet["total_tokens"] == usage["total_tokens"] == 360
    assert opus["total_cost_usd"] + sonnet["total_cost_usd"] == pytest.approx(11.91)


def test_token_priced_bucket_matches_flat_cost(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-priced", agent_id=_AGENT_ID)

    # codex-native reports cumulative tokens but NO explicit cost, so cost is
    # computed from the running token totals. Stub pricing so the test does not
    # depend on the live catalog; assert the per-model cost bucket still tracks
    # the flat token-priced total (the elif-has_tokens branch feeds by_model).
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: {"stub": True},
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, pricing: float(usage.get("total_tokens", 0)) * 0.01,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, "cumulative_output_tokens": 50, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    # total_tokens = 200 (input) + 0 (cache) + 50 (output) = 250; cost = 2.50.
    assert usage["total_cost_usd"] == pytest.approx(2.50)
    assert usage["by_model"]["gpt-5.6"]["total_cost_usd"] == pytest.approx(2.50)


def test_token_report_without_cache_field_keeps_persisted_cache_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-miss", agent_id=_AGENT_ID)

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(
            input_per_token=2.5e-6,
            output_per_token=10e-6,
            cache_read_per_token=0.25e-6,
            cache_write_per_token=None,
        ),
    )

    # Cache-heavy turn: 100K cumulative input, 90K of them cache reads.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # Cache-miss turn: the cumulative cached count is unchanged, and the
    # report omits it. The persisted split must carry forward rather than
    # re-billing the 90K cache reads at the full input rate.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    expected_cost = (110_000 * 2.5 + 90_000 * 0.25 + 400 * 10) / 1_000_000
    assert usage["cache_read_input_tokens"] == 90_000
    assert usage["input_tokens"] == 110_000
    assert usage["total_tokens"] == 200_400
    assert usage["total_cost_usd"] == pytest.approx(expected_cost)
    bucket = usage["by_model"]["gpt-5.6"]
    assert bucket["input_tokens"] == 110_000
    assert bucket["cache_read_input_tokens"] == 90_000
    assert bucket["total_cost_usd"] == pytest.approx(expected_cost)


def test_explicit_zero_cache_count_replaces_persisted_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-zero", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # An explicit zero is a report, not an omission: it is honored as-is.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 120_000,
            "cumulative_cache_read_input_tokens": 0,
            "cumulative_output_tokens": 300,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 0
    assert usage["input_tokens"] == 120_000


def test_carried_cache_count_is_clamped_to_a_lowered_input_total(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-lowered", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # A lowered cumulative total without a cache count keeps the carried split
    # within the reported input, and later reports carry that clamped count.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 50_000, "cumulative_output_tokens": 250, "model": "gpt-5.6"},
        store,
    )
    assert _usage(store, conv.id)["cache_read_input_tokens"] == 50_000
    assert _usage(store, conv.id)["input_tokens"] == 0

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 50_000
    assert usage["input_tokens"] == 150_000


def _conversation_point_reads(statements: list[str]) -> list[str]:
    """Filter captured SQL to point reads of a single conversation row."""
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "conversations.id = " in where and "root_conversation_id" not in where:
            matches.append(normalized)
    return matches


@pytest.mark.asyncio
async def test_flush_with_stale_row_keeps_monotonic_clamp(db_uri: str) -> None:
    """A stale caller-held row must not weaken the forged-low-report clamp.

    The events route reads the conversation at request start (authorization)
    and hands that row to the usage flush for the read-only tree walks. If a
    concurrent report persists a higher cost AFTER that read, a forged low
    report on the stale-row request must still be a no-op: the own-usage
    persist baselines its monotonic clamp on its own FRESH read, never on the
    caller's row (otherwise the replay could roll the persisted/enforced cost
    back and double-count the daily rollup delta).
    """
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="stale-clamp", agent_id=_AGENT_ID)
    # The route's access-check row, read while the persisted cost was $0.
    stale_row = store.get_conversation(conv.id)
    assert stale_row is not None

    # A concurrent legitimate report lands after the access check.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)

    # The stale-row request then delivers a (forged or delayed) lower report.
    await _persist_external_session_usage(
        conv.id,
        SessionEventInput(
            type="external_session_usage",
            data={"cumulative_cost_usd": 2.0, "model": "m1"},
        ),
        store,
        stale_row,
    )

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_flush_with_supplied_row_skips_tree_root_rereads(db_uri: str) -> None:
    """The flush's read-only tree walks must reuse the supplied row's root.

    With the route's row in hand, the only conversation point read a flush
    may issue is the own-usage persist's fresh clamp baseline — the subtree
    roll-up and the ancestor publish take the root from the supplied row.
    """
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-reads", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)

    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 1, (
        f"flush issued {len(point_reads)} conversation point reads (expected "
        f"1: the persist's fresh clamp baseline); statements={point_reads}"
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_flush_telemetry_reports_the_callers_installation_id(db_uri: str) -> None:
    """The usage event names the posting machine from its request header only.

    The header is what makes the attribution free: the server never looks the
    host up, so a flush from a client that sends no header leaves the field
    unset rather than costing a read.
    """
    from unittest.mock import patch

    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-telemetry", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    events: list = []
    with patch(
        "omnigent.server.routes._sessions.orchestration._tel_emit",
        side_effect=events.append,
    ):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
            "inst-host-9",
        )
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 4.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
        )

    assert [e.host_installation_id for e in events] == ["inst-host-9", None]


def _reset_session_after_spending(
    store: SqlAlchemyConversationStore, *, spent: float = 10.0, policy: float | None = None
) -> str:
    """Create a session that spent ``spent`` and then crossed a context reset."""
    conv = store.create_conversation(title="reset-usage", agent_id=_AGENT_ID)
    report: dict = {"cumulative_cost_usd": spent, "model": "m1", "context_epoch": 0}
    if policy is not None:
        report["policy_cost_usd"] = policy
    _persist_native_cumulative_usage(conv.id, report, store)
    store.append_context_reset(conv.id, response_id="reset_1")
    return conv.id


def _spy_daily_cost(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    deltas: list[float] = []
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._record_daily_cost",
        lambda _conv, delta, _store: deltas.append(delta),
    )
    return deltas


def test_cost_keeps_growing_after_a_reset_restarts_the_reported_total(db_uri: str) -> None:
    """S restarts near 0 in the new conversation; its growth adds to prior spend."""
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store)

    _persist_native_cumulative_usage(
        conv_id, {"cumulative_cost_usd": 0.5, "model": "m1", "context_epoch": 1}, store
    )
    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(10.5)

    _persist_native_cumulative_usage(
        conv_id, {"cumulative_cost_usd": 2.0, "model": "m1", "context_epoch": 1}, store
    )
    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(12.0)


def test_policy_gate_figure_keeps_growing_across_a_reset(db_uri: str) -> None:
    """The enforcement total rebases on its own peak, not on the display total."""
    from omnigent.runtime.policies.builder import load_session_usage

    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0, policy=12.0)

    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.0,
            "policy_cost_usd": 3.0,
            "model": "m1",
            "context_epoch": 1,
        },
        store,
    )

    usage = _usage(store, conv_id)
    assert usage["total_cost_usd"] == pytest.approx(11.0)
    assert usage["policy_cost_usd"] == pytest.approx(15.0)
    assert load_session_usage(conv_id, store)["policy_cost_usd"] == pytest.approx(15.0)


def test_policy_base_falls_back_to_the_total_when_no_enforcement_figure_exists(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0)

    _persist_native_cumulative_usage(conv_id, {"policy_cost_usd": 1.0, "context_epoch": 1}, store)

    assert _usage(store, conv_id)["policy_cost_usd"] == pytest.approx(11.0)


@pytest.mark.parametrize(
    "first_report",
    [
        {"cumulative_cost_usd": 0.0, "policy_cost_usd": 0.0, "context_epoch": 1},
        {"cumulative_cost_usd": 0.0, "policy_cost_usd": 0.0},
        {"cumulative_cost_usd": 9.0, "policy_cost_usd": 12.0, "context_epoch": 0},
        {"cumulative_cost_usd": 10.0, "policy_cost_usd": 12.0, "context_epoch": 0},
    ],
    ids=["zero", "zero-without-epoch", "stale-below-total", "stale-equal-to-total"],
)
def test_first_snapshot_after_a_reset_equals_the_pre_reset_total_exactly(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, first_report: dict
) -> None:
    """Neither spend dropped nor double-counted: exact equality, for both figures."""
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0, policy=12.0)
    daily = _spy_daily_cost(monkeypatch)

    _persist_native_cumulative_usage(conv_id, {**first_report, "model": "m1"}, store)

    usage = _usage(store, conv_id)
    assert usage["total_cost_usd"] == 10.0
    assert usage["policy_cost_usd"] == 12.0
    assert usage["by_model"]["m1"]["total_cost_usd"] == 10.0
    assert sum(daily) == 0.0


def test_stale_report_is_dropped_and_cannot_inflate_later_growth(db_uri: str) -> None:
    """A late pre-reset S added to the base would stick forever through the clamp."""
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0, policy=12.0)

    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 10.0,
            "policy_cost_usd": 12.0,
            "cumulative_input_tokens": 500,
            "model": "m1",
            "context_epoch": 0,
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.0,
            "policy_cost_usd": 1.0,
            "model": "m1",
            "context_epoch": 1,
        },
        store,
    )

    usage = _usage(store, conv_id)
    assert usage["total_cost_usd"] == pytest.approx(11.0)
    assert usage["policy_cost_usd"] == pytest.approx(13.0)
    assert "input_tokens" not in usage


def test_two_resets_in_a_row_keep_every_conversations_spend(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0, policy=10.0)
    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 3.0, "policy_cost_usd": 3.0, "context_epoch": 1},
        store,
    )
    store.append_context_reset(conv_id, response_id="reset_2")
    # A third conversation that reports nothing changes neither base.
    store.append_context_reset(conv_id, response_id="reset_3")

    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 0.0, "policy_cost_usd": 0.0, "context_epoch": 3},
        store,
    )
    assert _usage(store, conv_id)["total_cost_usd"] == 13.0
    assert _usage(store, conv_id)["policy_cost_usd"] == 13.0

    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 2.0, "policy_cost_usd": 2.0, "context_epoch": 3},
        store,
    )
    usage = _usage(store, conv_id)
    assert usage["total_cost_usd"] == pytest.approx(15.0)
    assert usage["policy_cost_usd"] == pytest.approx(15.0)


def test_forged_low_report_after_a_reset_lowers_nothing(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0, policy=10.0)
    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 4.0, "policy_cost_usd": 4.0, "context_epoch": 1},
        store,
    )

    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 1.0, "policy_cost_usd": 1.0, "context_epoch": 1},
        store,
    )

    usage = _usage(store, conv_id)
    assert usage["total_cost_usd"] == pytest.approx(14.0)
    assert usage["policy_cost_usd"] == pytest.approx(14.0)


def test_reset_growth_reaches_buckets_and_rollup_once(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-model buckets and the daily rollup see real growth, never the base."""
    store = SqlAlchemyConversationStore(db_uri)
    conv_id = _reset_session_after_spending(store, spent=10.0)
    daily = _spy_daily_cost(monkeypatch)

    _persist_native_cumulative_usage(
        conv_id, {"cumulative_cost_usd": 1.0, "model": "m1", "context_epoch": 1}, store
    )
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_cost_usd": 3.0, "model": "m2", "context_epoch": 1}, store
    )

    usage = _usage(store, conv_id)
    assert usage["by_model"]["m1"]["total_cost_usd"] == pytest.approx(11.0)
    assert usage["by_model"]["m2"]["total_cost_usd"] == pytest.approx(2.0)
    assert sum(daily) == pytest.approx(3.0)


def test_cumulative_tokens_rebase_across_a_reset(db_uri: str) -> None:
    """Totals do not drop at the boundary, and buckets gain only new tokens."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="reset-tokens", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 1000,
            "cumulative_output_tokens": 200,
            "cumulative_cache_read_input_tokens": 400,
            "model": "m1",
        },
        store,
    )
    store.append_context_reset(conv.id, response_id="reset_1")

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "model": "m1",
            "context_epoch": 1,
        },
        store,
    )

    usage = _usage(store, conv.id)
    # The report omits cache reads, so none of the base's 400 carry into this
    # conversation: all 100 input tokens are uncached.
    assert usage["input_tokens"] == 700
    assert usage["cache_read_input_tokens"] == 400
    assert usage["output_tokens"] == 220
    assert usage["total_tokens"] == 1320
    assert usage["by_model"]["m1"]["input_tokens"] == 700
    assert usage["by_model"]["m1"]["total_tokens"] == 1320


def test_token_priced_cost_rebases_across_a_reset(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prior spend keeps its own price; only this conversation's tokens are priced."""
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing_with_provider",
        lambda *a, **kw: ModelPricing(input_per_token=0.01, output_per_token=0),
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="reset-priced", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_input_tokens": 1000, "model": "m1"}, store
    )
    store.append_context_reset(conv.id, response_id="reset_1")

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_input_tokens": 100, "model": "m1", "context_epoch": 1}, store
    )

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(11.0)


def test_never_reset_session_stores_no_base_and_accepts_a_current_epoch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="no-reset", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 2.0, "model": "m1", "context_epoch": 0}, store
    )
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 3.0, "model": "m1"}, store)

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(3.0)
    assert "context_reset_base" not in usage


@pytest.mark.parametrize("bad_epoch", ["1", True, -1])
def test_malformed_context_epoch_is_rejected(db_uri: str, bad_epoch: object) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="bad-epoch", agent_id=_AGENT_ID)

    with pytest.raises(OmnigentError):
        _persist_native_cumulative_usage(
            conv.id, {"cumulative_cost_usd": 1.0, "context_epoch": bad_epoch}, store
        )


def test_flush_cannot_erase_a_base_written_after_it_read_the_row(db_uri: str) -> None:
    """A reset landing between a flush's read and write keeps its base and epoch."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-race", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0}, store)
    real_get = store.get_conversation
    raced = False

    def get_then_reset(conversation_id: str, *args: object, **kwargs: object):
        nonlocal raced
        row = real_get(conversation_id, *args, **kwargs)
        if not raced:
            raced = True
            store.append_context_reset(conv.id, response_id="reset_1")
        return row

    store.get_conversation = get_then_reset  # type: ignore[method-assign]
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.0, "context_epoch": 0}, store
    )
    store.get_conversation = real_get  # type: ignore[method-assign]
    assert raced

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 1.0, "context_epoch": 1}, store
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(11.0)


def test_concurrent_flushes_cannot_lower_the_persisted_total(db_uri: str) -> None:
    """A flush that read before a higher report landed must not overwrite it."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="lost-update", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 1.0}, store)
    real_get = store.get_conversation
    raced = False

    def get_then_higher_report(conversation_id: str, *args: object, **kwargs: object):
        nonlocal raced
        row = real_get(conversation_id, *args, **kwargs)
        if not raced:
            raced = True
            _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 9.0}, store)
        return row

    store.get_conversation = get_then_higher_report  # type: ignore[method-assign]
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 5.0}, store)
    store.get_conversation = real_get  # type: ignore[method-assign]

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(9.0)


def test_codex_token_priced_total_survives_a_reset_and_grows_by_the_new_threads_turns(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reports tokens only; a fresh thread restarts them but the total keeps growing.

    The cost is the token-priced list-price estimate the badge already shows. Reports
    here are unstamped, as an older forwarder sends them.
    """
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing_with_provider",
        lambda model, **kwargs: {"stub": True},
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, pricing: float(usage.get("total_tokens", 0)) * 0.01,
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-reset", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 1000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(12.0)

    store.append_context_reset(conv.id, response_id="reset_1")
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(12.0)

    # The fresh thread's first turn adds only its own tokens.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "model": "gpt-5.6",
        },
        store,
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(13.2)

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 150,
            "cumulative_output_tokens": 30,
            "model": "gpt-5.6",
        },
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(13.8)
    assert usage["by_model"]["gpt-5.6"]["total_cost_usd"] == pytest.approx(13.8)


async def _codex_usage_post(
    epoch: int | None, input_tokens: int, output_tokens: int
) -> dict[str, object]:
    """Return the usage body the real codex coalescer posts at ``epoch``."""
    import httpx

    from omnigent.harnesses.codex_native.forwarder import _SessionUsageCoalescer

    bodies: list[dict[str, object]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content)["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_capture), base_url="http://ap.test"
    ) as client:
        coalescer = _SessionUsageCoalescer(client, "conv_x", context_epoch=epoch)
        coalescer.record(
            {
                "tokenUsage": {
                    "total": {
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "cachedInputTokens": 0,
                    }
                }
            },
            model="gpt-5.6",
        )
        await coalescer.flush()
    return bodies[-1]


async def test_late_codex_usage_from_before_a_reset_is_dropped_and_new_epoch_adds(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-late", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(conv.id, await _codex_usage_post(0, 1000, 200), store)
    store.append_context_reset(conv.id, response_id="reset_1")
    total_after_reset = _usage(store, conv.id)["total_tokens"]
    assert total_after_reset == 1200

    # The old forwarder flushes once more after the reset.
    _persist_native_cumulative_usage(conv.id, await _codex_usage_post(0, 1100, 220), store)
    assert _usage(store, conv.id)["total_tokens"] == total_after_reset

    _persist_native_cumulative_usage(conv.id, await _codex_usage_post(1, 100, 20), store)
    assert _usage(store, conv.id)["total_tokens"] == total_after_reset + 120

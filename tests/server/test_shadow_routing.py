"""Shadow routing: rules backend, smart adapter, decision log, and create hook."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omnigent.server import shadow_routing as sr
from omnigent.server.smart_routing import RoutingResult
from tests.server.helpers import create_test_agent


def _request(message: str, **kwargs: object) -> sr.DecisionRequest:
    return sr.DecisionRequest(message=message, session_id="s1", **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_rules_keyword_assigns_bench_role() -> None:
    decision = await sr.RulesShadowRouter().decide(_request("Run the benchmark suite"))
    assert decision is not None
    assert (decision.role, decision.backend) == ("Bench", "rules")


@pytest.mark.asyncio
async def test_rules_short_question_picks_cheapest_model() -> None:
    models = {"claude-sdk": ["haiku", "sonnet", "opus"]}
    request = _request("What is a mutex?", available_models=models)
    decision = await sr.RulesShadowRouter().decide(request)
    assert decision is not None
    assert (decision.tier, decision.model, decision.harness) == ("cheap", "haiku", "claude-sdk")


@pytest.mark.asyncio
async def test_rules_no_match_and_custom_file(tmp_path: Path) -> None:
    no_match = await sr.RulesShadowRouter().decide(_request("Refactor the parser module please"))
    assert no_match is None
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n- name: gpu\n  match: {any_keywords: [train]}\n  decision: {host_needs: [gpu]}\n"
    )
    router = sr.RulesShadowRouter(sr.load_rules(rules_file))
    decision = await router.decide(_request("train the model"))
    assert decision is not None
    assert decision.host_needs == ("gpu",)


def test_load_rules_falls_back_to_defaults(tmp_path: Path) -> None:
    assert sr.load_rules(tmp_path / "missing.yaml") == sr.DEFAULT_RULES["rules"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [unclosed")
    assert sr.load_rules(bad) == sr.DEFAULT_RULES["rules"]


@pytest.mark.asyncio
async def test_smart_adapter_wraps_routing_client() -> None:
    class Client:
        async def route(
            self, message: str, available_models: dict[str, list[str]]
        ) -> RoutingResult | None:
            return RoutingResult(model="m1", rationale="because", harness="codex")

    adapter = sr.SmartRoutingShadowRouter(Client())
    decision = await adapter.decide(_request("hi", available_models={"codex": ["m1"]}))
    assert decision is not None
    assert (decision.model, decision.harness, decision.backend) == ("m1", "codex", "smart")
    assert await adapter.decide(_request("hi")) is None


def test_log_roundtrip_skips_corrupt_and_caps_size(tmp_path: Path) -> None:
    path = tmp_path / "routing" / "decisions.jsonl"
    sr.append_decision(
        {"ts": 1, "session_id": "a", "decision": {"rationale": "x" * 100_000}}, path
    )
    sr.append_decision({"ts": 2, "session_id": "b"}, path)
    with path.open("a") as handle:
        handle.write("not json\n\n")
    records = list(sr.iter_decisions(path))
    assert [record["session_id"] for record in records] == ["a", "b"]
    assert records[0]["truncated"] is True
    assert max(len(line) for line in path.read_text().splitlines()) <= sr.MAX_RECORD_BYTES
    assert list(sr.iter_decisions(tmp_path / "none.jsonl")) == []


def test_oversized_record_keeps_identity_and_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(sr, "oversized_records", 0)
    huge = {"schema_version": 1, "ts": 1, "decision_id": "d1", "session_id": "a"}
    sr.append_decision({**huge, "actual": {"model": "x" * 100_000}}, path)
    (record,) = sr.iter_decisions(path)
    assert (record["decision_id"], record["truncated"]) == ("d1", "dropped")
    assert sr.oversized_records == 1


class _NamedBackend:
    def __init__(self, name: str) -> None:
        self.name = name

    async def decide(self, request: sr.DecisionRequest) -> sr.ExecutionDecision | None:
        return sr.ExecutionDecision(backend=self.name, rationale="r")


@pytest.mark.asyncio
async def test_backends_share_one_decision_id(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    request = _request("hi", alias="Bench", project="p1")
    await sr.run_shadow_routing(request, [_NamedBackend("a"), _NamedBackend("b")], path=path)
    records = list(sr.iter_decisions(path))
    assert [r["backend"] for r in records] == ["a", "b"]
    assert {r["decision_id"] for r in records} == {request.decision_id}
    assert (records[0]["role"], records[0]["namespace"]) == ("Bench", "p1")


def test_pinned_dimensions_reports_explicit_fields() -> None:
    class Body:
        agent_id = None
        executor = None
        harness_override = "codex"
        model_override = None
        reasoning_effort = "high"
        host_id = None
        host_preference = "hpz4"

    assert sr.pinned_dimensions(Body()) == ["harness", "effort", "host"]


class _RaisingBackend:
    name = "boom"

    async def decide(self, request: sr.DecisionRequest) -> sr.ExecutionDecision | None:
        raise RuntimeError("nope")


class _SlowBackend:
    name = "slow"

    async def decide(self, request: sr.DecisionRequest) -> sr.ExecutionDecision | None:
        await asyncio.sleep(5)
        return None


@pytest.mark.asyncio
async def test_run_swallows_backend_errors_and_timeouts(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    await sr.run_shadow_routing(
        _request("hi"), [_RaisingBackend(), _SlowBackend()], path=path, timeout_s=0.05
    )
    assert list(sr.iter_decisions(path)) == []


async def _drain() -> None:
    while sr._background_tasks:
        await asyncio.gather(*list(sr._background_tasks))


@pytest.mark.asyncio
async def test_create_with_shadow_logs_one_record_and_response_is_unchanged(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    agent = await create_test_agent(client, name="shadow-agent")
    body = {
        "agent_id": agent["id"],
        "initial_items": [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "What is 2+2?"}],
                },
            }
        ],
    }
    monkeypatch.delenv(sr.SHADOW_ENV_VAR, raising=False)
    baseline = await client.post("/v1/sessions", json=body)
    await _drain()
    assert list(sr.iter_decisions()) == []

    monkeypatch.setenv(sr.SHADOW_ENV_VAR, "rules")
    shadowed = await client.post("/v1/sessions", json=body)
    await _drain()
    assert shadowed.status_code == baseline.status_code == 201
    volatile = {
        "id",
        "created_at",
        "updated_at",
        "items",
        "labels",
        "title",
        "root_conversation_id",
    }

    def stable(payload: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in payload.items() if key not in volatile}

    assert stable(shadowed.json()) == stable(baseline.json())

    records = list(sr.iter_decisions())
    assert len(records) == 1
    assert records[0]["session_id"] == shadowed.json()["id"]
    assert records[0]["backend"] == "rules"
    assert records[0]["decision"]["tier"] == "cheap"
    assert set(records[0]["actual"]) == {
        "agent_id",
        "harness",
        "model",
        "reasoning_effort",
        "host_id",
    }
    record = records[0]
    assert (record["schema_version"], record["site"]) == (1, "session_create")
    assert record["step_id"] == record["session_id"]
    assert "agent" in record["pinned"] and "model" not in record["pinned"]


@pytest.mark.asyncio
async def test_create_unaffected_by_raising_backend(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(sr.SHADOW_ENV_VAR, "rules")
    monkeypatch.setattr(
        sr, "build_shadow_routers", lambda names, routing_client: [_RaisingBackend()]
    )
    agent = await create_test_agent(client, name="shadow-boom")
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    await _drain()
    assert response.status_code == 201
    assert list(sr.iter_decisions()) == []


@pytest.mark.asyncio
async def test_rules_keyword_matches_whole_words_only() -> None:
    router = sr.RulesShadowRouter()
    assert await router.decide(_request("fix the retrieval pipeline please now")) is None
    assert await router.decide(_request("run the eval please")) is not None


@pytest.mark.asyncio
async def test_rules_match_with_only_unknown_keys_never_fires() -> None:
    router = sr.RulesShadowRouter([{"name": "typo", "match": {"keywords": ["x"]}, "decision": {}}])
    assert await router.decide(_request("x")) is None


class _StubClient:
    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
        harness = next(iter(available_models))
        return RoutingResult(model=available_models[harness][0], rationale="stub", harness=harness)


def _fake_session(**fields: object) -> object:
    return type("Fake", (), fields)()


@pytest.mark.asyncio
async def test_hook_fills_available_models_so_smart_backend_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(sr.SHADOW_ENV_VAR, "smart")
    monkeypatch.setattr(
        "omnigent.server.routing_backend.backends_from_caps",
        lambda caps: type("B", (), {"any": lambda self: _StubClient()})(),
    )
    monkeypatch.setattr("omnigent.runtime.get_caps", lambda: object())
    body = _fake_session(initial_input=None, initial_items=(), alias=None, project_id=None)
    resp = _fake_session(id="s9", harness="claude-sdk")
    sr.shadow_route_created_session(body, resp)
    await _drain()
    records = list(sr.iter_decisions())
    assert [r["backend"] for r in records] == ["smart"]
    assert records[0]["decision"]["harness"] == "claude-sdk"


@pytest.mark.asyncio
async def test_hook_bundled_result_uses_session_id_and_harness_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(sr.SHADOW_ENV_VAR, "rules")
    event = _fake_session(data={"content": "run the benchmark now"})
    body = _fake_session(initial_input=event, initial_items=(), alias=None, host_id="h1")
    result = _fake_session(session_id="b1", agent_id="a1")
    sr.shadow_route_created_session(body, result, harness="codex")
    await _drain()
    (record,) = list(sr.iter_decisions())
    assert record["session_id"] == "b1"
    assert (record["actual"]["harness"], record["actual"]["host_id"]) == ("codex", "h1")


@pytest.mark.asyncio
async def test_create_unaffected_when_scheduling_fails(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(sr.SHADOW_ENV_VAR, "rules,smart")

    def boom() -> object:
        raise RuntimeError("caps unavailable")

    monkeypatch.setattr("omnigent.runtime.get_caps", boom)
    agent = await create_test_agent(client, name="shadow-sched")
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    await _drain()
    assert response.status_code == 201
    assert list(sr.iter_decisions()) == []

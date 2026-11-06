"""A usage-limit notice during a failed Codex compaction keeps the compaction card."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
async def test_usage_limit_text_during_compaction_failure_keeps_compaction_card(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _session_id, event: published.append(event),
    )
    agent = await create_test_agent(
        client, executor={"type": "omnigent", "config": {"harness": "codex-native"}}
    )
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": "codex-native-ui"}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    posted = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {
                "status": "failed",
                "output": "You've hit your usage limit.",
                "compaction_model": "system.ai.gpt-6-astra",
                "selected_model": "system.ai.gpt-6-1-sol",
            },
        },
    )
    assert posted.status_code == 202, posted.text

    failed = [event for event in published if event.get("status") == "failed"]
    assert [event["error"]["code"] for event in failed] == ["codex_compaction_model_failed"]
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.json()["last_task_error"]["code"] == "codex_compaction_model_failed"

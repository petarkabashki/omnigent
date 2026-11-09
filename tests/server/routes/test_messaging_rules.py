"""Owner routes for the messaging-rules file (``/v1/messaging-rules``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.server.auth import delegated_path_allowed

_HANDLER = "omnigent.policies.builtins.messaging.messaging_rules"
_RULES = """# keep me
default: ask
rules:
  - from: a/*
    to: b/*
    action: allow
"""


@pytest_asyncio.fixture()
async def client(policy_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _enable(client: httpx.AsyncClient, path: Path) -> None:
    resp = await client.post(
        "/v1/policies",
        json={
            "name": "messaging_rules",
            "type": "python",
            "handler": _HANDLER,
            "factory_params": {"path": str(path)},
        },
    )
    assert resp.status_code == 200, resp.text


async def test_routes_404_without_an_enabled_policy(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/messaging-rules")).status_code == 404
    assert (await client.put("/v1/messaging-rules", json={"text": ""})).status_code == 404


async def test_get_returns_path_text_and_parse_status(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(_RULES)
    await _enable(client, path)
    body = (await client.get("/v1/messaging-rules")).json()
    assert (body["path"], body["text"], body["valid"], body["error"]) == (
        str(path),
        _RULES,
        True,
        None,
    )
    path.write_text("rules: 3\n")
    body = (await client.get("/v1/messaging-rules")).json()
    assert body["valid"] is False and "list" in body["error"]


async def test_put_replaces_valid_text_and_rejects_invalid(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(_RULES)
    await _enable(client, path)
    bad = await client.put(
        "/v1/messaging-rules",
        json={"text": "rules:\n  - {from: a, to: b, action: allow, until: nope}\n"},
    )
    assert bad.status_code == 400 and "until" in bad.text
    assert path.read_text() == _RULES
    good = "default: deny\n# note\n"
    assert (await client.put("/v1/messaging-rules", json={"text": good})).status_code == 200
    assert path.read_text() == good


async def test_test_endpoint_reports_action_and_rule(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(_RULES)
    await _enable(client, path)
    hit = (await client.post("/v1/messaging-rules/test", json={"from": "a/X", "to": "b/Y"})).json()
    assert (hit["action"], hit["rule_index"]) == ("ALLOW", 1)
    miss = (
        await client.post("/v1/messaging-rules/test", json={"from": "z/X", "to": "b/Y"})
    ).json()
    assert (miss["action"], miss["rule_index"]) == ("ASK", None)
    draft = {"from": "z/X", "to": "b/Y", "text": "default: deny\n"}
    assert (await client.post("/v1/messaging-rules/test", json=draft)).json()["action"] == "DENY"


@pytest.mark.parametrize("path", ["/v1/messaging-rules", "/v1/messaging-rules/test"])
def test_scoped_runner_and_client_tokens_cannot_reach_the_routes(path: str) -> None:
    assert not delegated_path_allowed(path)

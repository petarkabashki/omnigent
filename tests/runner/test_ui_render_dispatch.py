"""ui_render: advertised to native and local dispatch, validated and counted through /v1/views."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
    should_dispatch_locally,
)

SPEC = {
    "spec": 1,
    "view": {"type": "List", "source": {"name": "sessions"}, "item": {"title": "$.title"}},
}


def test_ui_render_is_advertised_to_native_and_local_dispatch() -> None:
    schema = next(
        tool for tool in build_native_relay_tool_schemas(None) if tool["name"] == "ui_render"
    )
    assert schema["parameters"]["required"] == ["spec"]
    assert should_dispatch_locally("ui_render")


async def _render(handler, arguments: str = json.dumps({"spec": SPEC})) -> dict:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        return json.loads(
            await execute_tool(
                tool_name="ui_render",
                arguments=arguments,
                conversation_id="current",
                server_client=client,
            )
        )


@pytest.mark.asyncio
async def test_ui_render_returns_the_normalized_spec_and_row_count() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path == "/v1/views/validate":
            assert body == {"spec": SPEC}
            normalized = json.loads(json.dumps(SPEC))
            normalized["view"]["source"].update(order="-last_event_ts", limit=100)
            return httpx.Response(200, json={"spec": normalized})
        assert body["source"]["order"] == "-last_event_ts"
        return httpx.Response(200, json={"rows": [{"id": "a"}, {"id": "b"}], "truncated": False})

    output = await _render(handler)

    assert calls == ["/v1/views/validate", "/v1/views/resolve"]
    assert (output["rows"], output["truncated"]) == (2, False)
    assert output["spec"]["view"]["source"]["limit"] == 100


@pytest.mark.asyncio
async def test_ui_render_reports_an_invalid_spec_without_resolving() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/views/validate"
        return httpx.Response(400, json={"error": "invalid view spec: view: bad type"})

    output = await _render(handler)
    assert output["error"] == "invalid view spec"
    assert "bad type" in output["detail"]


@pytest.mark.asyncio
async def test_ui_render_needs_server_access() -> None:
    output = json.loads(await execute_tool(tool_name="ui_render", arguments='{"spec": {}}'))
    assert "requires server access" in output["error"]

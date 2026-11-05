"""Runner-originated interrupts carry ``origin: agent`` so they are not owner overrides."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner import subagent_work
from omnigent.runner.tool_dispatch import _execute_rest_tool, execute_tool


def _recording_client(posts: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append({"url": request.url.path, "json": json.loads(request.content)})
        return httpx.Response(202, json={"queued": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.asyncio
async def test_sys_cancel_task_interrupt_declares_agent_origin() -> None:
    subagent_work.register_subagent_work(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        agent="worker",
        title="t",
    )
    posts: list[dict[str, Any]] = []
    try:
        async with _recording_client(posts) as client:
            output = await execute_tool(
                tool_name="sys_cancel_task",
                arguments=json.dumps({"task_id": "conv_child"}),
                conversation_id="conv_parent",
                server_client=client,
                session_async_tasks={},
            )
    finally:
        subagent_work.unregister_subagent_work("conv_child")
    assert posts, output
    assert posts == [
        {
            "url": "/v1/sessions/conv_child/events",
            "json": {"type": "interrupt", "data": {"origin": "agent"}},
        }
    ]


@pytest.mark.asyncio
async def test_sys_cancel_async_rest_dispatch_declares_agent_origin() -> None:
    posts: list[dict[str, Any]] = []
    async with _recording_client(posts) as client:
        await _execute_rest_tool("sys_cancel_async", {"handle_id": "conv_child"}, client)
    assert posts == [
        {
            "url": "/v1/sessions/conv_child/events",
            "json": {"type": "interrupt", "data": {"origin": "agent"}},
        }
    ]

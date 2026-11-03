"""Unit tests for the skip-gzip opt-out in ``_gzip_route``."""

from __future__ import annotations

import pytest
from starlette.types import Message, Receive, Scope, Send

from omnigent.server.routes._gzip_route import SKIP_GZIP_STATE_KEY, _StateAwareGZipResponder


async def _run_responder(*, skip: bool, body: bytes) -> list[Message]:
    """Drive the responder with a stub app and return what reached the client."""
    state: dict[str, object] = {SKIP_GZIP_STATE_KEY: True} if skip else {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request"}

    scope: Scope = {"type": "http", "headers": [(b"accept-encoding", b"gzip")], "state": state}
    responder = _StateAwareGZipResponder(app, 1024, 4, state)
    await responder(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_skip_gzip_sends_start_before_body() -> None:
    """A handler opt-out must still emit the response start (PDF reads returned 500)."""
    body = b"A" * 5000
    sent = await _run_responder(skip=True, body=body)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == body
    assert b"content-encoding" not in {k for k, _ in sent[0]["headers"]}


@pytest.mark.asyncio
async def test_without_skip_large_body_is_compressed() -> None:
    """The opt-out is the only path that bypasses compression."""
    sent = await _run_responder(skip=False, body=b"A" * 5000)

    assert sent[0]["type"] == "http.response.start"
    assert (b"content-encoding", b"gzip") in sent[0]["headers"]

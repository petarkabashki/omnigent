"""Python client methods for read-only external task rows."""

from __future__ import annotations

import json

import httpx
import pytest
from omnigent_client import OmnigentError, SessionsNamespace


def _namespace(handler: httpx.MockTransport) -> SessionsNamespace:
    return SessionsNamespace(httpx.AsyncClient(transport=handler), "http://test")


def _row(**overrides: object) -> dict[str, object]:
    return {
        "id": "abc",
        "title": "fk-gate-7",
        "status": "running",
        "state": "running",
        "exit_code": None,
        "detail": None,
        "unit": "fk-gate-7",
        "host_id": "h1",
        "project_id": None,
        "labels": {"omnigent.external.unit": "fk-gate-7"},
        **overrides,
    }


async def test_create_update_delete_external_task_requests() -> None:
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(201, json=_row())
        if request.method == "PATCH":
            return httpx.Response(200, json=_row(status="failed", state="failed", exit_code=2))
        return httpx.Response(200, json={"id": "abc", "deleted": True})

    sessions = _namespace(httpx.MockTransport(handler))

    created = await sessions.create_external_task(
        "fk-gate-7", "fk-gate-7", host_id="h1", status="running"
    )
    updated = await sessions.update_external_task("abc", "failed", exit_code=2)
    await sessions.delete("abc")

    assert created.created is True and created.unit == "fk-gate-7"
    assert updated.status == "failed" and updated.exit_code == 2 and updated.created is False
    assert seen == [
        (
            "POST",
            "/v1/external-tasks",
            {"title": "fk-gate-7", "unit": "fk-gate-7", "host_id": "h1", "status": "running"},
        ),
        ("PATCH", "/v1/external-tasks/abc", {"status": "failed", "exit_code": 2}),
        ("DELETE", "/v1/sessions/abc", None),
    ]


async def test_create_external_task_repeat_reports_existing_and_errors_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["unit"] == "bad":
            return httpx.Response(404, json={"error": {"message": "Project not found"}})
        return httpx.Response(200, json=_row())

    sessions = _namespace(httpx.MockTransport(handler))

    existing = await sessions.create_external_task("t", "u")
    with pytest.raises(OmnigentError):
        await sessions.create_external_task("t", "bad")

    assert existing.created is False

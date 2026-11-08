"""
Integration tests for ``/v1/projects/{id}/shells``.

A real host tunnel + projects + project-shells routers, with a fake host that
keeps a shell table and answers ``host.project_shell`` / ``host.shell_ws``
frames the way the host process does.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HOST_CAPABILITIES,
    HostHelloFrame,
    HostProjectShellFrame,
    HostProjectShellResultFrame,
    HostShellWsFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    decode_frame,
    encode_frame,
)
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.project_shells import create_project_shells_router
from omnigent.server.routes.projects import create_projects_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import websocket_scope as _websocket_scope

pytestmark = [pytest.mark.asyncio, pytest.mark.flaky(reruns=2, reruns_delay=1)]

_HOST_ID = "a38abbfd54a8b22204184b69d9bfd49f"


@dataclass
class _FakeHost:
    """Host stand-in: a shell table plus a record of every project-shell request."""

    comm: ApplicationCommunicator
    shells: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[HostProjectShellFrame] = field(default_factory=list)
    drain: asyncio.Task[None] | None = None

    def creates(self) -> list[HostProjectShellFrame]:
        return [r for r in self.requests if r.op == "create"]

    async def serve(self) -> None:
        counter = 0
        while True:
            output = await self.comm.receive_output(timeout=None)
            text = output.get("text") if output.get("type") == "websocket.send" else None
            if not isinstance(text, str):
                continue
            try:
                frame = decode_host_frame(text)
            except ValueError:
                continue
            if isinstance(frame, HostProjectShellFrame):
                self.requests.append(frame)
                counter += 1
                result = self._answer(frame, counter)
                await self._send(encode_host_frame(result))
            elif isinstance(frame, HostShellWsFrame):
                await self._echo(frame)

    def _answer(self, frame: HostProjectShellFrame, counter: int) -> HostProjectShellResultFrame:
        if frame.op == "create":
            shell = {
                "id": f"pshell_{counter}",
                "project_id": frame.project_id,
                "title": f"Shell {counter}",
                "created_at": 1,
                "alive": True,
            }
            self.shells[shell["id"]] = shell
            return HostProjectShellResultFrame(frame.request_id, "ok", [shell])
        if frame.op == "list":
            wanted = [
                s for s in self.shells.values() if frame.project_id in (None, s["project_id"])
            ]
            return HostProjectShellResultFrame(frame.request_id, "ok", wanted)
        self.shells.pop(frame.shell_id or "", None)
        return HostProjectShellResultFrame(frame.request_id, "ok")

    async def _echo(self, envelope: HostShellWsFrame) -> None:
        inner = decode_frame(envelope.payload)
        if isinstance(inner, WSFrame):
            data = (
                base64.b64decode(inner.data) if inner.encoding == "base64" else inner.data.encode()
            )
            reply = WSFrame(
                ch_id=inner.ch_id,
                data=base64.b64encode(b"echo:" + data).decode(),
                encoding="base64",
            )
            await self._send(encode_host_frame(HostShellWsFrame(encode_frame(reply))))
        elif isinstance(inner, WSOpenFrame) and "missing" in inner.path:
            close = WSCloseFrame(ch_id=inner.ch_id, code=4404, reason="shell not found")
            await self._send(encode_host_frame(HostShellWsFrame(encode_frame(close))))

    async def _send(self, text: str) -> None:
        await self.comm.send_input({"type": "websocket.receive", "text": text})


@dataclass
class _Env:
    app: FastAPI
    project_store: SqlAlchemyProjectStore
    registry: HostRegistry
    host: _FakeHost


def _build_app(db_uri: str, auth: UnifiedAuthProvider | None = None) -> tuple[FastAPI, Any]:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    project_store = SqlAlchemyProjectStore(db_uri)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.state.host_registry = registry
    app.state.host_store = host_store
    app.include_router(
        create_host_tunnel_router(registry, host_store, project_store=project_store),
        prefix="/v1",
    )
    app.include_router(
        create_projects_router(
            project_store=project_store,
            auth_provider=auth,
            conversation_store=SqlAlchemyConversationStore(db_uri),
        ),
        prefix="/v1",
    )
    app.include_router(create_project_shells_router(project_store, auth), prefix="/v1")
    return app, (registry, project_store)


async def _connect_host(
    app: FastAPI, registry: HostRegistry, capabilities: list[str]
) -> _FakeHost:
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    assert (await comm.receive_output(timeout=1.0))["type"] == "websocket.accept"
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="shell-host",
        capabilities=capabilities,
    )
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    host = _FakeHost(comm)
    host.drain = asyncio.create_task(host.serve())
    return host


async def _teardown(host: _FakeHost) -> None:
    assert host.drain is not None
    host.drain.cancel()
    with suppress(asyncio.CancelledError):
        await host.drain
    await host.comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await host.comm.wait(timeout=5.0)


@pytest.fixture()
async def env(db_uri: str) -> AsyncIterator[_Env]:
    app, (registry, project_store) = _build_app(db_uri)
    host = await _connect_host(app, registry, list(HOST_CAPABILITIES))
    try:
        yield _Env(app, project_store, registry, host)
    finally:
        await _teardown(host)


def _project(env: _Env, name: str = "p", **config: Any) -> str:
    config = {"host_id": _HOST_ID, "workspace": "/work/app", **config}
    return env.project_store.create(uuid.uuid4().hex, name, None, config=config).id


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_list_close_round_trip(env: _Env) -> None:
    project_id = _project(env)
    async with _client(env.app) as client:
        created = await client.post(f"/v1/projects/{project_id}/shells", json={})
        assert created.status_code == 201, created.text
        shell = created.json()
        assert shell["host_id"] == _HOST_ID and shell["alive"] is True
        # The host is asked to run in the project's default workspace.
        assert env.host.creates()[0].workspace == "/work/app"

        listed = await client.get(f"/v1/projects/{project_id}/shells")
        assert [s["id"] for s in listed.json()["data"]] == [shell["id"]]

        closed = await client.delete(f"/v1/projects/{project_id}/shells/{shell['id']}")
        assert closed.status_code == 200
        assert (await client.get(f"/v1/projects/{project_id}/shells")).json()["data"] == []


async def test_create_rejects_a_simple_cross_site_request(env: _Env) -> None:
    project_id = _project(env)
    async with _client(env.app) as client:
        response = await client.post(
            f"/v1/projects/{project_id}/shells",
            content="{}",
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code in (400, 415)
    assert env.host.creates() == []


async def test_missing_default_host_or_workspace_is_400(env: _Env) -> None:
    no_host = env.project_store.create(uuid.uuid4().hex, "a", None, config={"workspace": "/w"}).id
    no_workspace = env.project_store.create(
        uuid.uuid4().hex, "b", None, config={"host_id": _HOST_ID}
    ).id
    async with _client(env.app) as client:
        for project_id, word in ((no_host, "host"), (no_workspace, "workspace")):
            response = await client.post(f"/v1/projects/{project_id}/shells", json={})
            assert response.status_code == 400
            assert f"default {word}" in response.text
    assert env.host.creates() == []


async def test_offline_host_is_503(env: _Env) -> None:
    project_id = _project(env, host_id="ab" * 16)
    async with _client(env.app) as client:
        # An unknown host row is a 404; a known but disconnected one is a 503.
        assert (await client.post(f"/v1/projects/{project_id}/shells", json={})).status_code == 404
        env.registry.deregister(_HOST_ID)
        online_project = _project(env, name="q")
        response = await client.post(f"/v1/projects/{online_project}/shells", json={})
    assert response.status_code == 503, response.text


async def test_host_without_capability_gets_update_hint(db_uri: str) -> None:
    app, (registry, project_store) = _build_app(db_uri)
    host = await _connect_host(app, registry, [])
    try:
        project = project_store.create(
            uuid.uuid4().hex, "x", None, config={"host_id": _HOST_ID, "workspace": "/w"}
        )
        async with _client(app) as client:
            response = await client.post(f"/v1/projects/{project.id}/shells", json={})
        assert response.status_code == 409
        assert "update omnigent" in response.text
    finally:
        await _teardown(host)


async def test_shells_are_owner_only(db_uri: str) -> None:
    auth = UnifiedAuthProvider(source="header")
    app, (_registry, project_store) = _build_app(db_uri, auth)
    project = project_store.create(
        uuid.uuid4().hex,
        "mine",
        "alice@example.com",
        config={"host_id": _HOST_ID, "workspace": "/w"},
    )
    async with _client(app) as client:
        bob = {"X-Forwarded-Email": "bob@example.com"}
        for method, suffix in (("GET", ""), ("POST", ""), ("DELETE", "/pshell_1")):
            response = await client.request(
                method, f"/v1/projects/{project.id}/shells{suffix}", headers=bob, json={}
            )
            assert response.status_code == 404, (method, response.text)


async def test_project_delete_closes_its_shells(env: _Env) -> None:
    doomed, kept = _project(env, "doomed"), _project(env, "kept")
    async with _client(env.app) as client:
        await client.post(f"/v1/projects/{doomed}/shells", json={})
        survivor = (await client.post(f"/v1/projects/{kept}/shells", json={})).json()
        assert (await client.delete(f"/v1/projects/{doomed}")).status_code == 200
    assert list(env.host.shells) == [survivor["id"]]


async def test_host_reconnect_closes_shells_of_deleted_projects(db_uri: str) -> None:
    app, (registry, project_store) = _build_app(db_uri)
    live_id = project_store.create(uuid.uuid4().hex, "live", None, config={}).id
    host = await _connect_host(app, registry, list(HOST_CAPABILITIES))
    try:
        await asyncio.sleep(0)
    finally:
        await _teardown(host)
    # A host that still holds shells for a gone project and one that exists.
    second = _FakeHost(
        await _reconnecting_comm(app, registry),
        shells={
            "pshell_old": {"id": "pshell_old", "project_id": uuid.uuid4().hex, "alive": True},
            "pshell_ok": {"id": "pshell_ok", "project_id": live_id, "alive": True},
        },
    )
    second.drain = asyncio.create_task(second.serve())
    try:
        async with asyncio.timeout(5):
            while "pshell_old" in second.shells:
                await asyncio.sleep(0.02)
        assert list(second.shells) == ["pshell_ok"]
    finally:
        await _teardown(second)


async def _reconnecting_comm(app: FastAPI, registry: HostRegistry) -> ApplicationCommunicator:
    while registry.get(_HOST_ID) is not None:
        await asyncio.sleep(0.01)
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    await comm.receive_output(timeout=1.0)
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="shell-host",
        capabilities=list(HOST_CAPABILITIES),
    )
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    return comm


async def _browser(app: FastAPI, path: str) -> ApplicationCommunicator:
    browser = ApplicationCommunicator(app, _websocket_scope(path))
    await browser.send_input({"type": "websocket.connect"})
    return browser


async def test_attach_relays_bytes_through_the_host_tunnel(env: _Env) -> None:
    project_id = _project(env)
    async with _client(env.app) as client:
        shell = (await client.post(f"/v1/projects/{project_id}/shells", json={})).json()
    browser = await _browser(env.app, f"/v1/projects/{project_id}/shells/{shell['id']}/attach")
    try:
        assert (await browser.receive_output(timeout=2.0))["type"] == "websocket.accept"
        await browser.send_input({"type": "websocket.receive", "bytes": b"ls\r"})
        output = await browser.receive_output(timeout=2.0)
        assert output["bytes"] == b"echo:ls\r"
    finally:
        await browser.send_input({"type": "websocket.disconnect", "code": 1000})
        await browser.wait(timeout=5.0)


async def test_attach_mirrors_the_hosts_close_code(env: _Env) -> None:
    project_id = _project(env)
    browser = await _browser(env.app, f"/v1/projects/{project_id}/shells/missing/attach")
    assert (await browser.receive_output(timeout=2.0))["type"] == "websocket.accept"
    closed = await browser.receive_output(timeout=2.0)
    assert closed["type"] == "websocket.close" and closed["code"] == 4404
    await browser.wait(timeout=5.0)

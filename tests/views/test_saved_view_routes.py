"""Saved view routes: owner-only CRUD, YAML exchange and resolve as the caller."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes.saved_views import create_saved_views_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.saved_view_store import SqlAlchemySavedViewStore

OWNER = "alice"
OTHER = "bob"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
SPEC = {
    "spec": 1,
    "view": {
        "type": "List",
        "source": {"name": "sessions", "params": {"search": "$text"}},
        "item": {"title": "$.title"},
    },
}
VIEW = {
    "name": "Deploys",
    "params": {"text": {"type": "string", "default": "deploy"}},
    "spec": SPEC,
}


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-user")


def _as(user: str) -> dict[str, str]:
    return {"x-user": user}


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, OTHER):
        permissions.ensure_user(user)
    return SimpleNamespace(
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        projects=SqlAlchemyProjectStore(db_uri),
    )


@pytest.fixture()
def client(stores: SimpleNamespace) -> TestClient:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=error.http_status)

    app.include_router(
        create_saved_views_router(
            stores.conversations,
            stores.projects,
            auth_provider=_HeaderAuth(),
            permission_store=stores.permissions,
        ),
        prefix="/v1",
    )
    return TestClient(app)


def _session(stores: SimpleNamespace, title: str, owner: str) -> str:
    conversation = stores.conversations.create_conversation(title=title, agent_id=AGENT_ID)
    stores.permissions.grant(owner, conversation.id, LEVEL_OWNER)
    return conversation.id


def test_a_second_user_cannot_list_read_change_delete_export_or_resolve_the_view(
    client: TestClient,
) -> None:
    created = client.post("/v1/views", json=VIEW, headers=_as(OWNER))
    assert created.status_code == 200, created.text
    view_id = created.json()["id"]

    assert client.get("/v1/views", headers=_as(OTHER)).json() == {"views": []}
    for method, path in [
        ("GET", f"/v1/views/{view_id}"),
        ("GET", f"/v1/views/{view_id}/export"),
        ("GET", f"/v1/views/{view_id}/resolve"),
        ("DELETE", f"/v1/views/{view_id}"),
    ]:
        assert client.request(method, path, headers=_as(OTHER)).status_code == 404, path
    renamed = client.put(f"/v1/views/{view_id}", json={"name": "x"}, headers=_as(OTHER))
    assert renamed.status_code == 404
    assert client.get(f"/v1/views/{view_id}", headers=_as(OWNER)).json()["name"] == "Deploys"
    # An import lands in the importer's own views; the same name is free for them.
    exported = client.get(f"/v1/views/{view_id}/export", headers=_as(OWNER)).text
    imported = client.post("/v1/views/import", json={"yaml": exported}, headers=_as(OTHER))
    assert imported.status_code == 200
    assert imported.json()["id"] != view_id
    assert [
        view["id"] for view in client.get("/v1/views", headers=_as(OWNER)).json()["views"]
    ] == [view_id]


def test_resolve_binds_query_params_and_returns_only_the_callers_rows(
    stores: SimpleNamespace, client: TestClient
) -> None:
    _session(stores, "deploy one", OWNER)
    _session(stores, "build two", OWNER)
    _session(stores, "deploy secret", OTHER)
    view_id = client.post("/v1/views", json=VIEW, headers=_as(OWNER)).json()["id"]

    default = client.get(f"/v1/views/{view_id}/resolve", headers=_as(OWNER)).json()
    build = client.get(f"/v1/views/{view_id}/resolve?p.text=build", headers=_as(OWNER)).json()

    assert [row["title"] for row in default["rows"]] == ["deploy one"]
    assert default["spec"]["view"]["source"]["params"] == {"search": "deploy"}
    assert [row["title"] for row in build["rows"]] == ["build two"]
    for query in ("p.nope=1", "p.text=a&p.text=b"):
        bad = client.get(f"/v1/views/{view_id}/resolve?{query}", headers=_as(OWNER))
        assert bad.status_code == 400, query


def test_export_carries_the_query_only_and_imports_back(client: TestClient) -> None:
    view_id = client.post("/v1/views", json=VIEW, headers=_as(OWNER)).json()["id"]

    exported = client.get(f"/v1/views/{view_id}/export", headers=_as(OWNER))
    assert exported.headers["content-type"].startswith("application/yaml")
    assert "rows" not in exported.text

    clash = client.post("/v1/views/import", json={"yaml": exported.text}, headers=_as(OWNER))
    copy = client.post(
        "/v1/views/import", json={"yaml": exported.text, "name": "Copy"}, headers=_as(OWNER)
    )
    assert clash.status_code == 409
    assert copy.status_code == 200
    assert {key: copy.json()[key] for key in VIEW} == {**VIEW, "name": "Copy"}


@pytest.mark.parametrize(
    "body",
    [
        {**VIEW, "name": " "},
        {**VIEW, "params": {}},
        {**VIEW, "params": {"text": {"type": "bool"}}},
        {**VIEW, "spec": {**SPEC, "sql": "1"}},
        {**VIEW, "project_id": 7},
    ],
)
def test_create_refuses_invalid_views(client: TestClient, body: dict) -> None:
    assert client.post("/v1/views", json=body, headers=_as(OWNER)).status_code == 400


def test_update_revalidates_the_whole_view_and_refuses_a_name_clash(client: TestClient) -> None:
    view_id = client.post("/v1/views", json=VIEW, headers=_as(OWNER)).json()["id"]
    client.post("/v1/views", json={**VIEW, "name": "Other"}, headers=_as(OWNER))

    dropped = client.put(f"/v1/views/{view_id}", json={"params": {}}, headers=_as(OWNER))
    clash = client.put(f"/v1/views/{view_id}", json={"name": "Other"}, headers=_as(OWNER))
    ok = client.put(f"/v1/views/{view_id}", json={"description": "d"}, headers=_as(OWNER))

    assert (dropped.status_code, clash.status_code) == (400, 409)
    assert ok.json()["description"] == "d"
    assert ok.json()["updated_at"] is not None


def test_views_filed_under_a_project_must_be_the_callers_project(
    stores: SimpleNamespace, client: TestClient
) -> None:
    mine = stores.projects.create(uuid.uuid4().hex, "mine", OWNER)
    theirs = stores.projects.create(uuid.uuid4().hex, "theirs", OTHER)

    filed = client.post("/v1/views", json={**VIEW, "project_id": mine.id}, headers=_as(OWNER))
    foreign = client.post(
        "/v1/views", json={**VIEW, "name": "x", "project_id": theirs.id}, headers=_as(OWNER)
    )
    listed = client.get(f"/v1/views?project_id={mine.id}", headers=_as(OWNER)).json()

    assert filed.status_code == 200
    assert foreign.status_code == 404
    assert [view["name"] for view in listed["views"]] == ["Deploys"]


def test_delete_removes_the_view(client: TestClient) -> None:
    view_id = client.post("/v1/views", json=VIEW, headers=_as(OWNER)).json()["id"]

    assert client.delete(f"/v1/views/{view_id}", headers=_as(OWNER)).status_code == 200
    assert client.get(f"/v1/views/{view_id}", headers=_as(OWNER)).status_code == 404


def test_single_user_mode_keeps_names_unique(db_uri: str) -> None:
    store = SqlAlchemySavedViewStore(db_uri)
    first = store.create(name="A", spec=SPEC, user_id=None)

    with pytest.raises(OmnigentError, match="already exists"):
        store.create(name="A", spec=SPEC, user_id=None)
    assert [view.id for view in store.list_owned(user_id=None)] == [first.id]


def test_deeply_nested_specs_and_imports_are_a_400(client: TestClient) -> None:
    deep: object = 1
    for _ in range(900):
        deep = [deep]
    nested_yaml = "version: 1\nname: deep\nspec: {view: " + "[" * 5000 + "]" * 5000 + "}\n"

    created = client.post(
        "/v1/views", json={**VIEW, "spec": {**SPEC, "x": deep}}, headers=_as(OWNER)
    )
    imported = client.post("/v1/views/import", json={"yaml": nested_yaml}, headers=_as(OWNER))

    assert (created.status_code, imported.status_code) == (400, 400)
    assert "nested too deeply" in created.json()["error"]
    assert "nested too deeply" in imported.json()["error"]


def test_param_strings_are_capped_and_a_refused_sample_says_to_give_a_default(
    client: TestClient,
) -> None:
    source = {"name": "sessions", "params": {"kind": "$kind"}}
    spec = {**SPEC, "view": {**SPEC["view"], "source": source}}
    no_default = {"name": "Kinds", "params": {"kind": {"type": "string"}}, "spec": spec}
    with_default = {**no_default, "params": {"kind": {"type": "string", "default": "session"}}}
    long_enum = {**VIEW, "params": {"text": {"type": "string", "enum": ["x" * 501]}}}

    refused = client.post("/v1/views", json=no_default, headers=_as(OWNER))

    assert refused.status_code == 400
    assert "give those params a default" in refused.json()["error"]
    assert client.post("/v1/views", json=with_default, headers=_as(OWNER)).status_code == 200
    assert client.post("/v1/views", json=long_enum, headers=_as(OWNER)).status_code == 400

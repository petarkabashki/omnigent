"""Hub-backed view sources: agentic-base row fields, the caller's scoping, filters and caps."""

from __future__ import annotations

import dataclasses
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.permissions import check_session_access
from omnigent.server.routes.views import create_views_router
from omnigent.server.session_aliases import set_session_alias
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from omnigent.views import sources as sources_module
from omnigent.views.sources import HubSources, SourceError, order_rows
from omnigent.views.spec import SPEC_ADAPTER

OWNER = "alice"
STRANGER = "mallory"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
# agentic_base.query field sets (agentic-base 763fa65).
SESSION_FIELDS = {
    "id", "hub_ref", "title", "kind", "harness", "spawn_kind", "parent_actor_id", "project_id",
    "project", "host_id", "host", "stopped", "stop_reason", "roles", "last_event_ts",
}  # fmt: skip
THREAD_FIELDS = {"id", "hub_ref", "kind", "title", "state", "members", "messages", "last_event_ts"}
MEMBER_FIELDS = {"address_id", "kind", "handle", "actor_id", "role_id"}
ROLE_FIELDS = {"id", "handle", "permanent", "holder_actor_id", "holder_hub_ref", "last_event_ts"}


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    projects = SqlAlchemyProjectStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (OWNER, STRANGER):
        permissions.ensure_user(user)
    return SimpleNamespace(
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        projects=projects,
    )


def _session(
    stores: SimpleNamespace, title: str, *, owner: str | None = OWNER, **create: object
) -> str:
    """A session; ``owner=None`` leaves it without a grant, like hub-spawned sub-agents."""
    conversation = stores.conversations.create_conversation(
        title=title, agent_id=AGENT_ID, **create
    )
    if owner is not None:
        stores.permissions.grant(owner, conversation.id, LEVEL_OWNER)
    return conversation.id


def _sources(stores: SimpleNamespace, **kwargs: object) -> HubSources:
    def can_read(conversation) -> bool:
        return check_session_access(
            OWNER, conversation.id, LEVEL_READ, stores.permissions, stores.conversations
        )

    return HubSources(stores.conversations, stores.projects, OWNER, can_read, **kwargs)


def _resolve(stores: SimpleNamespace, source: dict[str, object]) -> list[dict[str, object]]:
    spec = SPEC_ADAPTER.validate_python(
        {"spec": 1, "view": {"type": "List", "source": source, "item": {"title": "$.title"}}}
    )
    return _sources(stores).resolve(spec.view.source).rows


def test_session_rows_carry_every_agentic_base_field_and_only_the_callers_sessions(
    stores: SimpleNamespace,
) -> None:
    project_id = uuid.uuid4().hex
    stores.projects.create(project_id, "Omni", OWNER)
    mine = _session(stores, "mine", labels={"omnigent.wrapper": "claude-code-native-ui"})
    assert stores.conversations.set_conversation_project(mine, project_id)
    _session(stores, "theirs", owner=STRANGER)

    rows = _resolve(stores, {"name": "sessions"})

    assert [row["title"] for row in rows] == ["mine"]
    [row] = rows
    assert set(row) == SESSION_FIELDS
    assert row["hub_ref"] == {"origin": "hub", "kind": "session", "id": mine}
    assert (row["kind"], row["harness"], row["project"]) == (
        "session",
        "claude-code-native-ui",
        "Omni",
    )
    assert (row["stopped"], row["stop_reason"], row["spawn_kind"]) == (False, None, None)
    assert row["last_event_ts"] > 10**12


def test_session_filters_match_agentic_base(stores: SimpleNamespace) -> None:
    project_id = uuid.uuid4().hex
    stores.projects.create(project_id, "Omni", OWNER)
    held = _session(stores, "Product desk")
    set_session_alias(
        stores.conversations,
        stores.projects,
        session=stores.conversations.get_conversation(held),
        user_id=OWNER,
        alias="Product",
    )
    closed = _session(stores, "done", labels={CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    archived = _session(stores, "old")
    stores.conversations.update_conversation_with_changes(archived, archived=True)
    in_project = _session(stores, "filed")
    assert stores.conversations.set_conversation_project(in_project, project_id)

    def titles(**params: object) -> set[str]:
        return {row["title"] for row in _resolve(stores, {"name": "sessions", "params": params})}

    assert titles(search="DESK") == {"Product desk"}
    assert titles(project="Omni") == titles(project=project_id) == {"filed"}
    assert titles(archived=True) == {"done", "old"}
    assert "done" not in titles(archived=False)
    rows = {row["title"]: row for row in _resolve(stores, {"name": "sessions"})}
    assert rows["done"]["stop_reason"] == "closed"
    assert rows["old"]["stop_reason"] == "archived"
    [role_row] = _resolve(stores, {"name": "sessions", "params": {"role": "/Product"}})
    assert role_row["roles"] == ["/Product"]
    assert rows["done"]["hub_ref"]["id"] == closed


def test_subagents_walk_the_tree_to_the_requested_depth(stores: SimpleNamespace) -> None:
    root = _session(stores, "root")
    child = _session(
        stores, "child", kind="sub_agent", parent_conversation_id=root, sub_agent_name="helper"
    )
    _session(stores, "grandchild", kind="sub_agent", parent_conversation_id=child)

    one = _resolve(stores, {"name": "subagents", "params": {"of": root}})
    two = _resolve(stores, {"name": "subagents", "params": {"of": root, "depth": 2}})

    assert [(row["title"], row["depth"]) for row in one] == [("helper", 1)]
    assert one[0]["spawn_kind"] == "subagent"
    assert one[0]["parent_actor_id"] == root
    searched = _resolve(stores, {"name": "sessions", "params": {"search": "HELP"}})
    assert [row["title"] for row in searched] == ["helper"]
    assert [(row["title"], row["depth"]) for row in two] == [("helper", 1), ("grandchild", 2)]
    assert set(two[1]) == SESSION_FIELDS | {"depth"}


def test_sub_agents_without_their_own_grant_are_read_through_their_root(
    stores: SimpleNamespace,
) -> None:
    root = _session(stores, "root")
    child = _session(stores, "child", owner=None, kind="sub_agent", parent_conversation_id=root)
    _session(stores, "grand", owner=None, kind="sub_agent", parent_conversation_id=child)
    theirs = _session(stores, "theirs", owner=STRANGER)
    _session(stores, "hidden", owner=None, kind="sub_agent", parent_conversation_id=theirs)

    tree = _resolve(stores, {"name": "subagents", "params": {"of": root, "depth": 2}})
    listed = _resolve(stores, {"name": "sessions", "params": {"kind": "subagent"}})
    every = _resolve(stores, {"name": "sessions"})

    assert [row["title"] for row in tree] == ["child", "grand"]
    assert sorted(row["title"] for row in listed) == ["child", "grand"]
    assert sorted(row["title"] for row in every) == ["child", "grand", "root"]


def test_subagents_of_another_users_session_is_not_found(stores: SimpleNamespace) -> None:
    theirs = _session(stores, "theirs", owner=STRANGER)
    # A share on a child of their session must not make the root readable.
    child = _session(stores, "child", owner=STRANGER, parent_conversation_id=theirs)
    stores.permissions.grant(OWNER, child, LEVEL_READ)
    with pytest.raises(SourceError):
        _sources(stores).subagents(of=theirs)


def test_scan_cap_spends_only_on_readable_rows_newest_first(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = [_session(stores, f"older-{index}") for index in range(4)]
    mine = _session(stores, "mine", owner=None, kind="sub_agent", parent_conversation_id=older[0])
    theirs = _session(stores, "theirs", owner=STRANGER)
    for index in range(6):
        _session(stores, f"t{index}", owner=None, kind="sub_agent", parent_conversation_id=theirs)
    newest = _session(stores, "newest", owner=None, kind="sub_agent", parent_conversation_id=mine)
    monkeypatch.setattr(sources_module, "MAX_SCANNED", 4)
    monkeypatch.setattr(sources_module, "_PAGE_SIZE", 2)

    listed = _sources(stores).sessions(kind="subagent")
    every = _sources(stores).sessions()

    # Test rows share a second of updated_at, so compare sets; the order breaks ties by id.
    assert {row["id"] for row in listed} == {newest, mine}
    assert {"newest", "mine"} <= {row["title"] for row in every}


def test_scan_cap_bounds_the_whole_resolve(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _session(stores, "root")
    for index in range(4):
        child = _session(stores, f"c{index}", parent_conversation_id=root)
        for grand in range(4):
            _session(stores, f"g{index}{grand}", parent_conversation_id=child)
    monkeypatch.setattr(sources_module, "MAX_SCANNED", 5)
    monkeypatch.setattr(sources_module, "_PAGE_SIZE", 2)
    spec = SPEC_ADAPTER.validate_python(
        {
            "spec": 1,
            "view": {
                "type": "Tree",
                "source": {"name": "subagents", "params": {"of": root, "depth": 2}},
                "item": {"title": "$.title"},
            },
        }
    )
    sources = _sources(stores)
    resolved = sources.resolve(spec.view.source)
    assert sources.scanned <= 5
    assert (len(resolved.rows) <= 5, resolved.truncated) == (True, True)


def test_roles_and_group_threads_have_their_agentic_base_fields(stores: SimpleNamespace) -> None:
    holder = _session(stores, "desk")
    set_session_alias(
        stores.conversations,
        stores.projects,
        session=stores.conversations.get_conversation(holder),
        user_id=OWNER,
        alias="Product",
    )
    thread = stores.projects.group_threads.create("Launch", user_id=OWNER)
    stores.projects.group_threads.add_member(thread.id, "role", "/Product", delivered_seq=0)

    [role] = _resolve(stores, {"name": "roles", "params": {"held": True}})
    assert set(role) == ROLE_FIELDS
    assert (role["handle"], role["holder_actor_id"]) == ("/Product", holder)
    assert role["holder_hub_ref"] == {"origin": "hub", "kind": "session", "id": holder}
    assert _resolve(stores, {"name": "roles", "params": {"held": False}}) == []

    [row] = _resolve(stores, {"name": "threads", "params": {"kind": "group"}})
    assert set(row) == THREAD_FIELDS
    assert row["hub_ref"] == {"origin": "hub", "kind": "thread", "id": thread.id}
    [member] = row["members"]
    assert set(member) == MEMBER_FIELDS
    assert (member["handle"], member["role_id"]) == ("/Product", role["id"])
    assert _resolve(stores, {"name": "threads", "params": {"member": "/Nobody"}}) == []


def test_order_puts_nulls_last_and_breaks_ties_by_id() -> None:
    rows = [{"id": "b", "t": 1}, {"id": "a", "t": None}, {"id": "c", "t": 1}, {"id": "d", "t": 2}]
    assert [row["id"] for row in order_rows(rows, "-t")] == ["d", "c", "b", "a"]
    assert [row["id"] for row in order_rows(rows, "t")] == ["b", "c", "d", "a"]


def test_default_order_stops_scanning_once_the_limit_is_filled(stores: SimpleNamespace) -> None:
    times = [10, 9, 8, 8, 6, 5, 4, 3, 2, 1]  # newest first; a tie straddles the cut at 3
    convs = [
        dataclasses.replace(
            stores.conversations.get_conversation(_session(stores, f"s{t}")), updated_at=t
        )
        for t in times
    ]

    def list_conversations(
        *, limit: int, after: str | None = None, **_: object
    ) -> SimpleNamespace:
        start = 0 if after is None else [conv.id for conv in convs].index(after) + 1
        data = convs[start : start + limit]
        return SimpleNamespace(data=data, has_more=start + limit < len(convs), last_id=data[-1].id)

    pages = SimpleNamespace(list_conversations=list_conversations)
    sources = HubSources(pages, None, OWNER, lambda _conv: True)
    spec = SPEC_ADAPTER.validate_python(
        {
            "spec": 1,
            "view": {
                "type": "List",
                "source": {"name": "sessions", "limit": 3},
                "item": {"title": "$.title"},
            },
        }
    )
    resolved = sources.resolve(spec.view.source)

    every = order_rows(sources.session_rows(convs), "-last_event_ts")
    assert [row["id"] for row in resolved.rows] == [row["id"] for row in every[:3]]
    assert (sources.scanned, resolved.truncated) == (8, True)


def test_limit_truncates_and_says_so(stores: SimpleNamespace) -> None:
    for index in range(3):
        _session(stores, f"s{index}")
    spec = SPEC_ADAPTER.validate_python(
        {
            "spec": 1,
            "view": {
                "type": "List",
                "source": {"name": "sessions", "limit": 2},
                "item": {"title": "$.title"},
            },
        }
    )
    resolved = _sources(stores).resolve(spec.view.source)
    assert (len(resolved.rows), resolved.truncated) == (2, True)


@pytest.fixture()
def client(stores: SimpleNamespace) -> TestClient:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _error(_request: Request, error: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=error.http_status)

    app.include_router(
        create_views_router(
            stores.conversations, stores.projects, permission_store=stores.permissions
        ),
        prefix="/v1",
    )
    return TestClient(app)


def test_resolve_route_returns_rows_and_refuses_a_bad_source(
    stores: SimpleNamespace, client: TestClient
) -> None:
    _session(stores, "one")
    ok = client.post("/v1/views/resolve", json={"source": {"name": "sessions"}})
    bad = client.post("/v1/views/resolve", json={"source": {"name": "sessions", "sql": "1"}})

    assert ok.status_code == 200
    assert ok.json()["origin"] == "hub"
    assert [row["title"] for row in ok.json()["rows"]] == ["one"]
    assert bad.status_code == 400
    assert "sql" in bad.json()["error"]


def test_resolve_route_reads_sub_agents_with_auth_off(
    stores: SimpleNamespace, client: TestClient
) -> None:
    root = _session(stores, "root")
    _session(stores, "child", owner=None, kind="sub_agent", parent_conversation_id=root)
    tree = client.post(
        "/v1/views/resolve", json={"source": {"name": "subagents", "params": {"of": root}}}
    )
    listed = client.post("/v1/views/resolve", json={"source": {"name": "sessions"}})

    assert [row["title"] for row in tree.json()["rows"]] == ["child"]
    assert sorted(row["title"] for row in listed.json()["rows"]) == ["child", "root"]


def test_validate_route_normalizes_a_string_spec_and_explains_a_bad_one(
    client: TestClient,
) -> None:
    view = {"type": "List", "source": {"name": "roles"}, "item": {"title": "$.handle"}}
    spec = json.dumps({"spec": 1, "view": view})
    ok = client.post("/v1/views/validate", json={"spec": spec})
    bad = client.post("/v1/views/validate", json={"spec": {"spec": 1, "view": {"type": "Chart"}}})

    assert ok.status_code == 200
    assert ok.json()["spec"]["view"]["source"]["order"] == "-last_event_ts"
    assert bad.status_code == 400
    assert "view" in bad.json()["error"]


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"[" * 60000, b'{"spec": NaN}', b"[1]", b"{" + b" " * 200_000 + b"}"],
    ids=["malformed", "deep", "nan", "not-object", "oversized"],
)
@pytest.mark.parametrize("route", ["validate", "resolve"])
def test_routes_refuse_bad_bodies_with_400(client: TestClient, route: str, body: bytes) -> None:
    response = client.post(
        f"/v1/views/{route}", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


def test_routes_require_a_json_content_type(client: TestClient) -> None:
    response = client.post(
        "/v1/views/resolve",
        content=b'{"source": {"name": "roles"}}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415

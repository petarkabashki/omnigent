"""Tests for filing sessions into first-class projects via the sessions API.

Exercises the two HTTP surfaces added on top of the projects Stage-1 store:

- ``PATCH /v1/sessions/{id}`` with ``project_id`` — move a session into a
  project (non-empty id), unfile it (``""``), leave unchanged (omitted). Filing
  is by project **id** (the client holds it after create/list).
- ``GET /v1/sessions?project=<name>`` — list a project's member sessions by
  **name**, dual-reading the first-class entity and the legacy ``omni_project``
  label; ``?project=`` (empty) lists unfiled sessions. The name→id resolution is
  server-side, so the client passes only the name it renders in the sidebar.

Both are owner-scoped because projects are owner-private: only the session
owner may file it, and only into a project they own. The multi-user tests use
header auth to prove the ownership boundary holds — a caller can neither file a
session into another owner's project nor into a session they only have
edit/read access to.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject
from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    UnifiedAuthProvider,
)
from omnigent.server.routes.projects import create_projects_router
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


def _ensure_agent(db_uri: str) -> None:
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID,
            name="test-agent",
            bundle_location=f"{AGENT_ID}/bundle",
        )


# ── Single-user mode (no auth) ───────────────────────────────────────────


def _single_user_app(db_uri: str) -> FastAPI:
    """Build an app (no auth provider) mounting sessions + projects at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    project_store = SqlAlchemyProjectStore(db_uri)
    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            project_store=project_store,
        ),
        prefix="/v1",
    )
    app.include_router(create_projects_router(project_store=project_store), prefix="/v1")
    return app


def test_file_and_unfile_session_single_user(db_uri: str) -> None:
    """PATCH project_id files the session; project_id="" unfiles it."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))

    project = client.post("/v1/projects", json={"name": "Work"}).json()

    # File it (by id — the client holds the id after create).
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]

    # Listing by project NAME returns it (dual-read resolves the name → id).
    listed = client.get("/v1/sessions?project=Work")
    assert [s["id"] for s in listed.json()["data"]] == [conv.id]

    # Archived folders resolve first-class membership and exclude live sessions.
    archived = client.patch(f"/v1/sessions/{conv.id}", json={"archived": True})
    assert archived.status_code == 200
    archived_list = client.get("/v1/sessions?project=Work&visibility=archived")
    assert archived_list.status_code == 200
    assert [row["id"] for row in archived_list.json()["data"]] == [conv.id]
    assert archived_list.json()["data"][0]["project_id"] == project["id"]
    assert client.get("/v1/sessions?project=Work&visibility=mine").json()["data"] == []
    assert client.patch(f"/v1/sessions/{conv.id}", json={"archived": False}).status_code == 200

    # Unfile it.
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": ""})
    assert resp.status_code == 200
    assert resp.json()["project_id"] is None

    # No longer in the project; now shows under unfiled (project="").
    assert client.get("/v1/sessions?project=Work").json()["data"] == []
    unfiled = client.get("/v1/sessions?project=")
    assert conv.id in [s["id"] for s in unfiled.json()["data"]]


def test_omitting_project_id_leaves_membership_unchanged(db_uri: str) -> None:
    """A PATCH that doesn't mention project_id must not clear the filing."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    # A title-only PATCH leaves project membership intact.
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"title": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]


def test_patch_response_omits_items(db_uri: str) -> None:
    """PATCH returns a slim snapshot: items stay empty even when the session has some."""
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=AGENT_ID)
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()

    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]
    # PATCH callers use only scalar fields; transcripts come from the items
    # endpoint, so the mutation response skips the expensive items read.
    assert resp.json()["items"] == []

    # The slimming is PATCH-specific — the GET snapshot still carries items.
    assert len(client.get(f"/v1/sessions/{conv.id}").json()["items"]) == 1


def test_file_into_nonexistent_project_404(db_uri: str) -> None:
    """Filing into a project id that doesn't exist is rejected (404)."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    resp = client.patch(
        f"/v1/sessions/{conv.id}", json={"project_id": "ffffffffffffffffffffffffffffffff"}
    )
    assert resp.status_code == 404


def test_explicit_null_project_id_is_rejected(db_uri: str) -> None:
    """A present-but-null project_id is invalid: only "" unfiles, and omitting
    the field leaves membership unchanged — so null must not silently unfile."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": None})
    assert resp.status_code == 400
    # Membership is untouched — the invalid PATCH didn't unfile it.
    snap = client.get(f"/v1/sessions/{conv.id}")
    assert snap.json()["project_id"] == project["id"]


def test_unfile_unknown_session_404(db_uri: str) -> None:
    """Unfiling a session with no metadata row is a 404, mirroring the file
    path — a PATCH that changed nothing must not report success."""
    _ensure_agent(db_uri)
    client = TestClient(_single_user_app(db_uri))
    resp = client.patch("/v1/sessions/" + "f" * 32, json={"project_id": ""})
    assert resp.status_code == 404


def test_fork_inherits_source_project(db_uri: str) -> None:
    """A fork of a filed session lands in the same project as its source."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    resp = client.post(f"/v1/sessions/{conv.id}/fork", json={})
    assert resp.status_code == 201
    fork = resp.json()
    assert fork["project_id"] == project["id"]

    # The project folder lists both the source and the fork.
    listed = client.get("/v1/sessions?project=Work")
    assert {s["id"] for s in listed.json()["data"]} == {conv.id, fork["id"]}


def test_fork_of_unfiled_session_stays_unfiled(db_uri: str) -> None:
    """Forking a session outside any project must not invent a filing."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))

    resp = client.post(f"/v1/sessions/{conv.id}/fork", json={})
    assert resp.status_code == 201
    assert resp.json()["project_id"] is None


def test_project_filter_excludes_other_projects(db_uri: str) -> None:
    """?project=<name> returns only that project's members, not another's."""
    _ensure_agent(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    a = conv_store.create_conversation(title="a", agent_id=AGENT_ID)
    b = conv_store.create_conversation(title="b", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    p1 = client.post("/v1/projects", json={"name": "P1"}).json()
    client.post("/v1/projects", json={"name": "P2"})
    client.patch(f"/v1/sessions/{a.id}", json={"project_id": p1["id"]})
    # File b under P2 by name via the legacy label, to prove the dual-read OR
    # branch surfaces label members under the same ?project= filter.
    conv_store.set_labels(b.id, {"omni_project": "P2"})

    assert [s["id"] for s in client.get("/v1/sessions?project=P1").json()["data"]] == [a.id]
    assert [s["id"] for s in client.get("/v1/sessions?project=P2").json()["data"]] == [b.id]


def test_project_filter_dual_reads_label_and_entity(db_uri: str) -> None:
    """Under one ?project=<name>, both a first-class member and a legacy
    label member surface together (the dual-read OR)."""
    _ensure_agent(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    entity_member = conv_store.create_conversation(title="entity", agent_id=AGENT_ID)
    label_member = conv_store.create_conversation(title="label", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{entity_member.id}", json={"project_id": project["id"]})
    conv_store.set_labels(label_member.id, {"omni_project": "Work"})

    listed = client.get("/v1/sessions?project=Work").json()["data"]
    assert {s["id"] for s in listed} == {entity_member.id, label_member.id}


# ── Multi-user mode (header auth) — ownership boundary ───────────────────


def _multi_user_app(db_uri: str) -> FastAPI:
    """Build a header-auth app mounting sessions + projects at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    auth = UnifiedAuthProvider(source="header")
    project_store = SqlAlchemyProjectStore(db_uri)
    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            auth_provider=auth,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            project_store=project_store,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_projects_router(project_store=project_store, auth_provider=auth),
        prefix="/v1",
    )
    return app


def _seed_owned_session(db_uri: str, owner: str, title: str = "s") -> str:
    """Create a session owned by ``owner`` (owner-level grant). Returns its id."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title=title, agent_id=AGENT_ID)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _hdr(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


def test_owner_can_file_own_session_into_own_project(db_uri: str) -> None:
    """The happy path under header auth: Alice files her session into her project."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, ALICE)
    client = TestClient(_multi_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Alice"}, headers=_hdr(ALICE)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}", json={"project_id": project["id"]}, headers=_hdr(ALICE)
    )
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]
    listed = client.get("/v1/sessions?project=Alice", headers=_hdr(ALICE))
    assert [s["id"] for s in listed.json()["data"]] == [conv_id]


def test_cannot_file_into_another_owners_project(db_uri: str) -> None:
    """Alice owns the session but the target project is Bob's — rejected 404,
    and the session stays unfiled."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, ALICE)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(ALICE),
    )
    assert resp.status_code == 404
    # Still unfiled — the rejected filing had no side effect.
    snap = client.get(f"/v1/sessions/{conv_id}", headers=_hdr(ALICE))
    assert snap.json()["project_id"] is None


def test_fork_of_shared_session_in_foreign_project_stays_unfiled(db_uri: str) -> None:
    """Alice forks Bob's shared session filed in Bob's project. Projects are
    owner-private, so her fork must not carry Bob's project id — a foreign id
    would surface in no folder view of hers."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()
    filed = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )
    assert filed.status_code == 200

    resp = client.post(f"/v1/sessions/{conv_id}/fork", json={}, headers=_hdr(ALICE))
    assert resp.status_code == 201
    assert resp.json()["project_id"] is None


def test_fork_keeps_own_project_multi_user(db_uri: str) -> None:
    """Under header auth, Bob's fork of his own filed session stays in his
    project (the ownership check resolves against the forker's id)."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()
    client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )

    resp = client.post(f"/v1/sessions/{conv_id}/fork", json={}, headers=_hdr(BOB))
    assert resp.status_code == 201
    assert resp.json()["project_id"] == bob_project["id"]


def test_editor_cannot_file_shared_session(db_uri: str) -> None:
    """Bob owns the session and shares EDIT with Alice; Alice may not file it
    into her own project — filing is owner-only."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    alice_project = client.post("/v1/projects", json={"name": "Alice"}, headers=_hdr(ALICE)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": alice_project["id"]},
        headers=_hdr(ALICE),
    )
    # Edit access is below owner, so the owner-only gate rejects with 403.
    assert resp.status_code == 403


def test_project_listing_is_owner_scoped(db_uri: str) -> None:
    """A session shared to Alice but filed by its owner Bob must not surface in
    Alice's ?project= listing, even when Alice has her own like-named project.
    The name→id resolution is owner-scoped, so "Shared" resolves to Alice's own
    (empty) project, never Bob's."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Shared"}, headers=_hdr(BOB)).json()
    # Alice owns a distinct project that happens to share the name.
    client.post("/v1/projects", json={"name": "Shared"}, headers=_hdr(ALICE))
    client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )

    # Bob sees his member session under his "Shared".
    bob_list = client.get("/v1/sessions?project=Shared", headers=_hdr(BOB))
    assert [s["id"] for s in bob_list.json()["data"]] == [conv_id]

    # Alice, asking for "Shared", resolves to HER project — the shared session
    # (Bob's) does not surface: the listing is owner-scoped.
    alice_list = client.get("/v1/sessions?project=Shared", headers=_hdr(ALICE))
    assert alice_list.json()["data"] == []


# ── Nested projects that share a leaf name ───────────────────────────────


def _two_sub_projects(client: TestClient) -> dict[str, dict]:
    """Create ``a/sub`` and ``b/sub``; return the four project objects by path."""
    made: dict[str, dict] = {}
    for path in ("a", "b"):
        made[path] = client.post("/v1/projects", json={"name": path}).json()
        made[f"{path}/sub"] = client.post(
            "/v1/projects", json={"name": "sub", "parent_id": made[path]["id"]}
        ).json()
    return made


def test_project_id_filter_separates_nested_projects_sharing_a_leaf_name(db_uri: str) -> None:
    """Members and the project list go by id, so ``a/sub`` and ``b/sub`` never mix."""
    _ensure_agent(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    first = conversations.create_conversation(title="one", agent_id=AGENT_ID)
    second = conversations.create_conversation(title="two", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    made = _two_sub_projects(client)
    for session, key in ((first, "a/sub"), (second, "b/sub")):
        client.patch(f"/v1/sessions/{session.id}", json={"project_id": made[key]["id"]})

    for session, key in ((first, "a/sub"), (second, "b/sub")):
        listed = client.get("/v1/sessions", params={"project_id": made[key]["id"]})
        assert [row["id"] for row in listed.json()["data"]] == [session.id]
    assert client.get("/v1/sessions", params={"project_id": made["a"]["id"]}).json()["data"] == []

    summaries = client.get("/v1/sessions/projects").json()
    subs = sorted((row["path"], row["id"]) for row in summaries if row["name"] == "sub")
    assert subs == sorted((key, made[key]["id"]) for key in ("a/sub", "b/sub"))
    assert {row["parent_id"] for row in summaries if row["name"] == "sub"} == {
        made["a"]["id"],
        made["b"]["id"],
    }


def test_alias_list_project_filter_resolves_a_path_to_one_node(db_uri: str) -> None:
    """``?project=a/sub`` lists only that node's aliases; a bare leaf name matches no root."""
    _ensure_agent(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    client = TestClient(_single_user_app(db_uri))
    made = _two_sub_projects(client)
    handles = {}
    for key in ("a/sub", "b/sub"):
        session = conversations.create_conversation(title=key, agent_id=AGENT_ID)
        client.patch(f"/v1/sessions/{session.id}", json={"project_id": made[key]["id"]})
        assert client.put(
            f"/v1/sessions/{session.id}/alias", json={"alias": "Architect"}
        ).is_success
        handles[key] = session.id

    listed = client.get("/v1/sessions/aliases", params={"project": "a/sub"}).json()["data"]
    assert [(row["session_id"], row["handle"]) for row in listed] == [
        (handles["a/sub"], "a/sub/Architect")
    ]
    assert client.get("/v1/sessions/aliases", params={"project": "sub"}).json()["data"] == []
    assert len(client.get("/v1/sessions/aliases").json()["data"]) == 2


def test_same_alias_is_allowed_in_nested_siblings_and_rejected_within_one(db_uri: str) -> None:
    """Alias uniqueness is scoped to the project id, not the leaf name."""
    _ensure_agent(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    client = TestClient(_single_user_app(db_uri))
    made = _two_sub_projects(client)
    sessions = {}
    for label, key in (("one", "a/sub"), ("two", "a/sub"), ("three", "b/sub")):
        session = conversations.create_conversation(title=label, agent_id=AGENT_ID)
        client.patch(f"/v1/sessions/{session.id}", json={"project_id": made[key]["id"]})
        sessions[label] = session.id
    put = lambda label: client.put(  # noqa: E731
        f"/v1/sessions/{sessions[label]}/alias", json={"alias": "Bench"}
    )
    assert put("one").status_code == 200
    assert put("three").status_code == 200
    assert put("two").status_code == 409


def test_resolve_endpoint_and_legacy_slash_projects(db_uri: str) -> None:
    """``/projects/resolve`` finds one node; legacy ``x/y`` names are found but stay leaf roots."""
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlProject
    from omnigent.db.utils import get_or_create_engine

    client = TestClient(_single_user_app(db_uri))
    made = _two_sub_projects(client)
    legacy_id = "5" * 32
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(SqlProject(id=legacy_id, name="x/y", user_id=None, created_at=1))
        session.commit()

    resolved = client.get("/v1/projects/resolve", params={"path": "A/Sub"})
    assert resolved.status_code == 200 and resolved.json()["id"] == made["a/sub"]["id"]
    assert client.get("/v1/projects/resolve", params={"path": "a/nope"}).status_code == 404
    flat = client.get("/v1/projects/resolve", params={"path": "x/y"})
    assert flat.status_code == 200 and flat.json()["id"] == legacy_id
    listed = client.get("/v1/projects", params={"path": "x/y"}).json()["data"]
    assert [row["id"] for row in listed] == [legacy_id]

    # A save that echoes the legacy name keeps working; a legacy project is never a parent.
    saved = client.patch(
        f"/v1/projects/{legacy_id}", json={"name": "x/y", "config": {"icon": "i"}}
    )
    assert saved.status_code == 200 and saved.json()["config"] == {"icon": "i"}
    child = client.post("/v1/projects", json={"name": "kid", "parent_id": legacy_id})
    assert child.status_code == 400
    moved = client.patch(f"/v1/projects/{made['b']['id']}", json={"parent_id": legacy_id})
    assert moved.status_code == 400


@pytest.mark.parametrize("label_session_first", [False, True])
def test_label_only_session_shares_the_alias_namespace_of_its_root_project(
    db_uri: str, label_session_first: bool
) -> None:
    """A filed member and a label-only member of ``Work`` cannot both hold ``Work/Bench``."""
    _ensure_agent(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    filed = conversations.create_conversation(title="filed", agent_id=AGENT_ID)
    client.patch(f"/v1/sessions/{filed.id}", json={"project_id": project["id"]})
    labelled = conversations.create_conversation(title="labelled", agent_id=AGENT_ID)
    conversations.set_labels(labelled.id, {PROJECT_LABEL_KEY: "Work"})
    first, second = (labelled, filed) if label_session_first else (filed, labelled)

    assert client.put(f"/v1/sessions/{first.id}/alias", json={"alias": "Bench"}).status_code == 200
    assert (
        client.put(f"/v1/sessions/{second.id}/alias", json={"alias": "Bench"}).status_code == 409
    )


def test_project_id_filter_rejects_a_malformed_id(db_uri: str) -> None:
    client = TestClient(_single_user_app(db_uri))
    response = client.get("/v1/sessions", params={"project_id": "not-a-uuid"})
    assert 400 <= response.status_code < 500, response.text


def test_project_routes_accept_a_dashed_id(db_uri: str) -> None:
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "n"}).json()
    dashed = str(uuid.UUID(project["id"]))
    renamed = client.patch(f"/v1/projects/{dashed}", json={"name": "m"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["path"] == "m"
    assert client.delete(f"/v1/projects/{dashed}").status_code == 200


def test_the_patch_migrate_slashes_describes_is_accepted(db_uri: str) -> None:
    """``PATCH {name, parent_id}`` for a printed step turns a legacy ``a/b`` into a tree node."""
    client = TestClient(_single_user_app(db_uri))
    legacy = client.post("/v1/projects", json={"name": "seed"}).json()
    with Session(get_or_create_engine(db_uri)) as session:
        session.execute(update(SqlProject).where(SqlProject.id == legacy["id"]).values(name="a/b"))
        session.commit()
    parent = client.post("/v1/projects", json={"name": "a"}).json()

    patched = client.patch(
        f"/v1/projects/{legacy['id']}", json={"name": "b", "parent_id": parent["id"]}
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["path"] == "a/b"

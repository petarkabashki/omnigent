"""Runner dispatch through authenticated routes and real ownership/sharing stores."""

import json
import time
from contextvars import ContextVar
from uuid import uuid4

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime.prompt import role_release_instruction
from omnigent.server import session_aliases
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.feature_flags import (
    FEATURES_ENV_VAR,
    Feature,
    FeatureFlags,
    resolve_feature_flags,
)
from omnigent.server.sharing_settings import write_peer_session_messaging_override
from omnigent.server.startup_inputs import RESENT_NOTICE, add_delivery_markers
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    CREATED_BY_SESSION_LABEL_KEY,
    EXECUTOR_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ROLE_ID_LABEL_KEY,
    ROLE_OVERLAY_LABEL_KEY,
    ROLE_RELEASED_BY_LABEL_KEY,
    ROLE_RELEASED_LABEL_KEY,
    ROLE_RELEASED_TO_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.role_store.sqlalchemy_store import SqlAlchemyRoleStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from tests.debug_log_helpers import capture_debug_rows
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_attribution import _bind_runner
from tests.server.integration.test_sessions_harness_override import _stub_late_runner_bind
from tests.server.integration.test_sessions_permissions import _create_session_as
from tests.server.integration.test_sessions_subagent_context import (
    two_replica_stack as two_replica_stack,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def orchestration_environment(monkeypatch, request, tmp_path):
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    features = getattr(request, "param", None)
    monkeypatch.setenv("OMNIGENT_FEATURES", f"sibling_sessions,{features or ''}")
    return features


@pytest.fixture
async def peers(auth_client, auth_app, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    auth_client.headers.update(headers)
    captured = _stub_late_runner_bind(monkeypatch)
    return source, target, captured


async def _send(client, source, target, message="Please inspect the PR", kind=None):
    arguments = {"session_id": target["id"], "message": message}
    if kind is not None:
        arguments["kind"] = kind
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id=source["id"],
        )
    )


@pytest.mark.parametrize("policy_denied", [False, True])
@pytest.mark.parametrize(
    "orchestration_environment",
    [None, "", "usage_page", "peer_session_messaging", "-session_orchestration"],
    indirect=True,
)
async def test_private_sender_can_message_shared_receiver(
    auth_client, peers, db_uri, monkeypatch, policy_denied, orchestration_environment
):
    from omnigent.server.routes.sessions import routes_events

    source, target, captured = peers
    # The server snapshot is already built; leave the runner at its default.
    monkeypatch.delenv(FEATURES_ENV_VAR, raising=False)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", target["id"], LEVEL_READ)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
    result = await _send(auth_client, source, target)
    if orchestration_environment == "-session_orchestration":
        assert "Session orchestration is disabled" in result["detail"]
        assert not captured.get("posts")
        return
    if policy_denied:
        assert "Task needs approval" in result["error"]
        assert result["denied"] is True and result["queued"] is False
        assert not captured.get("posts")
        return
    assert result.get("queued") is True, result
    assert "Please inspect the PR" in json.dumps(captured)
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from session {source['id']}, message {result['message_id']}]" in items.text
    assert '"created_by":"alice"' in items.text
    store = SqlAlchemyConversationStore(db_uri)
    assert store.get_conversation(target["id"]).parent_conversation_id is None
    assert store.get_conversation(source["id"]).parent_conversation_id is None


@pytest.mark.parametrize("runner_features", [None, "peer_session_messaging"])
async def test_server_can_disable_peer_messaging(
    auth_client, auth_app, peers, monkeypatch, runner_features
):
    source, target, captured = peers
    auth_app.state.feature_flags = resolve_feature_flags(
        {FEATURES_ENV_VAR: "-peer_session_messaging"}
    )
    if runner_features is not None:
        monkeypatch.setenv(FEATURES_ENV_VAR, runner_features)
    result = await _send(auth_client, source, target)
    assert "Messaging other sessions is disabled" in result["detail"], result
    assert not captured.get("posts")


async def test_admin_can_disable_and_restore_peer_messaging(auth_client, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("admin", is_admin=True)
    admin_headers = {"X-Forwarded-Email": "admin"}
    for enabled in (False, True):
        response = await auth_client.put(
            "/v1/sharing",
            headers=admin_headers,
            json={"peer_session_messaging": enabled},
        )
        assert response.status_code == 200, response.text
        captured.clear()
        result = await _send(auth_client, source, target)
        if enabled:
            assert result.get("queued") is True, result
        else:
            assert "Messaging other sessions is disabled" in result["detail"]
            assert not captured.get("posts")


async def test_runner_is_trusted_for_other_owned_sessions_it_hosts(auth_client, peers, db_uri):
    source, target, _ = peers
    sibling = await _create_session_as(auth_client, "", "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_runner_id(sibling["id"], store.get_conversation(source["id"]).runner_id)
    assert (await _send(auth_client, sibling, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", sibling["id"], LEVEL_EDIT)
    assert (
        "shared with edit access cannot" in (await _send(auth_client, sibling, target))["detail"]
    )


@pytest.mark.parametrize("batch", [False, True])
async def test_peer_message_tracks_runner_startup(auth_client, peers, monkeypatch, batch):
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration

    source, target, _ = peers
    started = []

    async def observe_startup(*args, **kwargs):
        started.append(orchestration._session_is_starting(target["id"]))

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", observe_startup)
    event = {"type": "session_message", "data": {"message": "Start"}}
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json=[event] if batch else event,
    )
    assert response.status_code == 202, response.text
    assert started == [True]
    assert not orchestration._dispatch_in_flight.get(target["id"])


@pytest.mark.parametrize(
    "condition",
    [
        "flag_off",
        "shared_edit",
        "public_edit",
        "other_owner",
        "admin",
        "missing_binding",
        "wrong_binding",
        "self",
        "blank",
        "oversize",
        "missing_source",
    ],
)
async def test_refusal_before_delivery(condition, auth_client, auth_app, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    if condition == "flag_off":
        auth_app.state.feature_flags = FeatureFlags()
    if condition in {"shared_edit", "public_edit"}:
        user = "__public__" if condition == "public_edit" else "bob"
        permissions.ensure_user(user)
        permissions.grant(user, source["id"], LEVEL_EDIT)
    if condition in {"other_owner", "admin"}:
        other = await _create_session_as(auth_client, "", "bob")
        permissions.grant("alice", other["id"], LEVEL_EDIT)
        target = other
        if condition == "admin":
            permissions.set_admin("alice", True)
    if condition == "missing_binding":
        del auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER]
    if condition == "wrong_binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    if condition == "self":
        target = source
    captured.clear()
    response = await auth_client.post(
        f"/v1/sessions/{target['id']}/events",
        headers={}
        if condition == "missing_source"
        else {ORCHESTRATION_SOURCE_HEADER: source["id"]},
        json={
            "type": "session_message",
            "data": {
                "message": {
                    "blank": " ",
                    "oversize": "a" * 8001,
                }.get(condition, "DO_NOT_DELIVER")
            },
        },
    )
    assert response.status_code in {400, 403}, response.text
    assert not captured.get("posts")


@pytest.mark.parametrize("manager_kind", ["root", "child", "side_chat"])
async def test_view_only_sharing_keeps_new_controls(auth_client, peers, db_uri, manager_kind):
    source, target, captured = peers
    manager = source
    if manager_kind != "root":
        response = await auth_client.post(
            f"/v1/sessions/{source['id']}/fork" if manager_kind == "side_chat" else "/v1/sessions",
            json={"side_chat": True}
            if manager_kind == "side_chat"
            else {"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        if manager_kind == "side_chat":
            _bind_runner(db_uri, manager["id"])
    permissions = SqlAlchemyPermissionStore(db_uri)
    for viewer in ("bob", "__public__"):
        permissions.ensure_user(viewer)
        permissions.grant(viewer, source["id"], LEVEL_READ)
        if manager_kind == "side_chat":
            permissions.grant(viewer, manager["id"], LEVEL_READ)
    captured.clear()
    assert (await _send(auth_client, manager, target)).get("queued") is True
    assert "Please inspect the PR" in json.dumps(captured)
    # Upgrading any viewer to edit restores the refusal.
    permissions.grant("bob", source["id"], LEVEL_EDIT)
    captured.clear()
    assert (
        "shared with edit access cannot" in (await _send(auth_client, manager, target))["detail"]
    )
    assert not captured.get("posts")


async def test_editor_beyond_first_grant_page_blocks_controls(auth_client, peers, db_uri):
    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    for index in range(100):
        permissions.ensure_user(f"viewer-{index:03d}")
        permissions.grant(f"viewer-{index:03d}", source["id"], LEVEL_READ)
    assert (await _send(auth_client, source, target)).get("queued") is True
    metadata = {"agent_id": source["agent_id"], "sibling_source_id": source["id"]}
    assert (await auth_client.post("/v1/sessions", json=metadata)).status_code == 201
    permissions.ensure_user("zed")
    permissions.grant("zed", source["id"], LEVEL_EDIT)
    _, cursor = permissions.list_for_session(source["id"])
    assert cursor is not None
    captured.clear()
    assert "shared with edit access cannot" in (await _send(auth_client, source, target))["detail"]
    assert not captured.get("posts")
    assert (await auth_client.post("/v1/sessions", json=metadata)).status_code == 403


@pytest.mark.parametrize("restriction", ["sharing", "admin_messaging"])
async def test_sharing_during_receiver_startup_blocks_delivery(
    auth_client, peers, db_uri, monkeypatch, restriction
):
    from omnigent.server.routes import sessions

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    async def share_while_starting(*args, **kwargs):
        if restriction == "sharing":
            permissions.grant("bob", source["id"], LEVEL_EDIT)
        else:
            write_peer_session_messaging_override(False)

    monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", share_while_starting)
    result = await _send(auth_client, source, target)
    assert "error" in result, result
    assert not any("/events" in path for path, _ in captured.get("posts", []))
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert "Please inspect" not in items.text


async def test_child_inherits_source_sharing(auth_client, peers, db_uri):
    source, target, captured = peers
    response = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "parent_session_id": source["id"],
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()
    assert (await _send(auth_client, child, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    permissions.grant("bob", source["id"], LEVEL_EDIT)
    captured.clear()
    assert "shared with edit access cannot" in (await _send(auth_client, child, target))["detail"]
    assert not captured.get("posts")


@pytest.mark.parametrize("descendant", ["side_chat", "nested_side_chat", "child"])
@pytest.mark.parametrize("share", ["source", "side_chat"])
async def test_side_chat_inherits_sharing_for_new_controls(
    auth_client, peers, db_uri, descendant, share
):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    manager = side
    if descendant != "side_chat":
        response = await auth_client.post(
            f"/v1/sessions/{side['id']}/fork"
            if descendant == "nested_side_chat"
            else "/v1/sessions",
            json={"side_chat": True}
            if descendant == "nested_side_chat"
            else {"agent_id": side["agent_id"], "parent_session_id": side["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        if descendant == "nested_side_chat":
            _bind_runner(db_uri, manager["id"])
    assert (await _send(auth_client, manager, target)).get("queued") is True
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    shared_id = source["id"] if share == "source" else side["id"]
    permissions.grant("bob", shared_id, LEVEL_EDIT)
    store = SqlAlchemyConversationStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    captured.clear()
    assert (
        "shared with edit access cannot" in (await _send(auth_client, manager, target))["detail"]
    )
    for placement in ("same_host", "managed"):
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": side["agent_id"], "placement": placement}),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        assert "shared with edit access cannot" in result["detail"], result
    assert not captured.get("posts")
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants
    # Sharing restricts new controls, not existing owner reads or incoming work.
    history = await auth_client.get(f"/v1/sessions/{manager['id']}/items")
    assert history.status_code == 200, history.text
    _bind_runner(db_uri, target["id"])
    assert (await _send(auth_client, target, manager)).get("queued") is True
    permissions.revoke("bob", shared_id)
    assert (await _send(auth_client, manager, target)).get("queued") is True


@pytest.mark.parametrize("source_link", ["missing", "cycle", "absent"])
async def test_side_chat_requires_resolvable_source(auth_client, peers, db_uri, source_link):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    store = SqlAlchemyConversationStore(db_uri)
    if source_link == "absent":
        store.delete_label(side["id"], SIDE_CHAT_SOURCE_LABEL_KEY)
    else:
        store.set_labels(
            side["id"],
            {SIDE_CHAT_SOURCE_LABEL_KEY: side["id"] if source_link == "cycle" else uuid4().hex},
        )
    captured.clear()
    assert (
        "Session is not owned or available" in (await _send(auth_client, side, target))["detail"]
    )
    assert not captured.get("posts")

    nested = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "sibling_source_id": side["id"]}
    )
    assert nested.status_code == 403, nested.text
    assert store.get_conversation(side["id"]).family_id is None


@pytest.mark.parametrize("execution_target", ["connected_host", "managed_sandbox"])
@pytest.mark.parametrize("full_fork", [False, True])
async def test_scheduled_origin_allows_new_controls_children_and_reads(
    auth_client, auth_app, peers, db_uri, monkeypatch, execution_target, full_fork
):
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.scheduled.fire import FireDeps, build_on_fire
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
    from tests._helpers.polling import poll_until_async

    source, target, captured = peers
    conversations = SqlAlchemyConversationStore(db_uri)
    schedules = SqlAlchemyScheduledTaskStore(db_uri)
    task = schedules.create(
        scheduled_task_id=uuid4().hex,
        name="Scheduled worker",
        prompt="Ordinary scheduled work",
        rrule="FREQ=HOURLY",
        user_id="alice",
        agent_id=source["agent_id"],
        timezone="UTC",
        execution_target=execution_target,
        host_id=uuid4().hex,
        workspace="/tmp",
    )
    labels_at_launch = []

    async def launch(session, task):
        labels_at_launch.append(session.labels)

    on_fire = build_on_fire(
        FireDeps(
            scheduled_task_store=schedules,
            agent_store=auth_app.state.agent_store,
            conversation_store=conversations,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            host_store=auth_app.state.host_store,
            host_registry=auth_app.state.host_registry,
            sandbox_config=auth_app.state.sandbox_config,
        ),
        launch_dispatch=launch,
    )
    assert await on_fire(0, task.id)
    await poll_until_async(lambda: schedules.get(task.id).last_run_conversation_id, timeout_s=5)
    scheduled_id = schedules.get(task.id).last_run_conversation_id
    assert labels_at_launch[0][SCHEDULED_TASK_ORIGIN_LABEL_KEY] == task.id
    schedules.delete(task.id)
    assert schedules.list_runs(task.id)[0] == []

    if full_fork:
        response = await auth_client.post(f"/v1/sessions/{scheduled_id}/fork", json={})
        assert response.status_code == 201, response.text
        scheduled_id = response.json()["id"]
        assert (
            conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
            == task.id
        )

    # Managed creation succeeds now; record its launch instead of provisioning.
    launched_by = []

    async def launch(**kwargs):
        launched_by.append(kwargs["owner"])

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    scheduled = {"id": scheduled_id}
    auth_client.headers.update(_bind_runner(db_uri, scheduled_id))
    for depth in range(3):
        captured.clear()
        # A scheduled run, its forks and children message the owner's sessions
        # like any private session they own.
        result = await _send(auth_client, scheduled, target)
        assert result.get("queued") is True, result
        assert "Please inspect the PR" in json.dumps(captured)
        for placement in ("same_host", "managed"):
            result = json.loads(
                await execute_tool(
                    tool_name="sys_session_create",
                    arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
                    server_client=auth_client,
                    conversation_id=scheduled["id"],
                )
            )
            if placement == "managed":
                assert result.get("conversation_id"), result
            else:
                # Same-host placement needs a live host this test does not
                # register; it must not fail on the scheduled origin.
                assert result.get("error") != "access_denied", result
        history = json.loads(
            await execute_tool(
                tool_name="sys_session_get_history",
                arguments=json.dumps({"conversation_id": target["id"]}),
                server_client=auth_client,
                conversation_id=scheduled["id"],
            )
        )
        assert "error" not in history, history
        # Ordinary child creation retains its existing authorization and parentage.
        if depth < 2:
            response = await auth_client.post(
                "/v1/sessions",
                json={"agent_id": source["agent_id"], "parent_session_id": scheduled["id"]},
            )
            assert response.status_code == 201, response.text
            scheduled = response.json()

    await poll_until_async(lambda: len(launched_by) == 3, timeout_s=5)
    assert launched_by == ["alice"] * 3
    for labels in (
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: ""},
        {SCHEDULED_TASK_ORIGIN_LABEL_KEY: "other"},
    ):
        response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{scheduled_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        conversations.get_conversation(scheduled_id).labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY]
        == task.id
    )


@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize("siblings_enabled", [False, True])
@pytest.mark.parametrize(
    "condition,orchestration_environment",
    [("flag_off", "-session_orchestration"), ("shared", None), ("binding", None)],
    indirect=["orchestration_environment"],
)
async def test_creation_refuses_before_launch(
    auth_client, auth_app, peers, db_uri, placement, condition, siblings_enabled
):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    if not siblings_enabled:
        auth_app.state.feature_flags = FeatureFlags(
            auth_app.state.feature_flags.enabled_features - {Feature.SIBLING_SESSIONS}
        )
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    assert auth_app.state.sandbox_config.managed_launch_supported
    if condition == "shared":
        permissions = SqlAlchemyPermissionStore(db_uri)
        permissions.ensure_user("bob")
        permissions.grant("bob", source["id"], LEVEL_EDIT)
    elif condition == "binding":
        auth_client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "wrong"
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": placement}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    expected = {
        "flag_off": "Session orchestration is disabled",
        "shared": "shared with edit access cannot",
        "binding": "requires the sender's bound runner",
    }[condition]
    assert expected in result["detail"], result
    if condition == "flag_off":
        response = await auth_client.post(
            "/v1/sessions",
            headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
            json={
                "agent_id": source["agent_id"],
                "host_type": "managed" if placement == "managed" else "external",
            },
        )
        assert response.status_code == 403 and expected in response.text
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


async def test_same_host_still_requires_host_ownership(auth_client, auth_app, peers, db_uri):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    host_id = "a" * 32
    _register_online_host(auth_app, host_id, "bob")
    SqlAlchemyConversationStore(db_uri).set_host_id(source["id"], host_id, workspace="/tmp")
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    before_sessions = {s.id for s in store.list_conversations().data}
    before_grants = permissions.list_for_user("alice")
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "same_host"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "not your host" in result["detail"], result
    assert not captured.get("posts")
    assert auth_app.state.host_registry.get(host_id).outbound_queue.empty()
    assert {s.id for s in store.list_conversations().data} == before_sessions
    assert permissions.list_for_user("alice") == before_grants


@pytest.mark.parametrize(
    "child_manager,move_before_recheck,policy_denied,source_viewers",
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (True, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
        (True, False, False, True),
    ],
)
async def test_same_host_creation_uses_current_source_and_stays_private(
    auth_client,
    auth_app,
    peers,
    db_uri,
    monkeypatch,
    move_before_recheck,
    child_manager,
    policy_denied,
    source_viewers,
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core, routes_events
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
    hosts = [uuid4().hex for _ in range(3)]
    for host_id, owner in zip(hosts, ("alice", "alice", "bob"), strict=True):
        _register_online_host(auth_app, host_id, owner)
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, owner)
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], hosts[0], workspace="/original")
    if source_viewers:
        viewers = SqlAlchemyPermissionStore(db_uri)
        for viewer in ("bob", "__public__"):
            viewers.ensure_user(viewer)
            viewers.grant(viewer, source["id"], LEVEL_READ)
    manager = source
    if child_manager:
        response = await auth_client.post(
            "/v1/sessions",
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert store.get_conversation(manager["id"]).host_id is None
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    original_check = routes_core.require_orchestration_source
    checks = 0

    def check_source(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2 and move_before_recheck:
            store.set_host_id(source["id"], hosts[1], workspace="/moved")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(routes_core, "require_orchestration_source", check_source)
    frames = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append((connection.host_id, frame))
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)
    if policy_denied:

        async def deny_task(*args, **kwargs):
            return {"verdict": "deny", "reason": "Task needs approval"}

        monkeypatch.setattr(routes_events, "_evaluate_input_policy", deny_task)
        before = {s.id for s in store.list_conversations().data}
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "message": "Start",
                    }
                ),
                server_client=auth_client,
                conversation_id=manager["id"],
            )
        )
        assert result["status"] == "created_initial_message_failed", result
        assert "Task needs approval" in result["error"]
        assert result["initial_message"]["denied"] is True
        assert all(path == "/v1/sessions" for path, _ in captured.get("posts", []))
        created_id = result["conversation_id"]
        assert {s.id for s in store.list_conversations().data} - before == {created_id}
    else:
        with capture_debug_rows("server") as rows:
            response = await auth_client.post(
                "/v1/sessions/orchestrated",
                headers={ORCHESTRATION_SOURCE_HEADER: manager["id"]},
                json={"agent_id": source["agent_id"]},
            )
        assert response.status_code == 201, response.text
        created_id = response.json()["id"]
        [created_row] = [
            row
            for row in rows
            if row["event_name"] == "session_created" and row["session_id"] == created_id
        ]
        assert created_row["attributes"]["created_by_session_id"] == manager["id"]
        assert "parent_session_id" not in created_row["attributes"]
        assert created_row["user_id"] == "alice"
    created = store.get_conversation(created_id)
    expected_host = hosts[1] if move_before_recheck else hosts[0]
    expected_workspace = "/moved" if move_before_recheck else "/original"
    assert checks == 2
    assert created.parent_conversation_id is None
    assert created.labels[CREATED_BY_SESSION_LABEL_KEY] == manager["id"]
    assert (created.host_id, created.workspace) == (expected_host, expected_workspace)
    assert frames and all(host == expected_host for host, _ in frames)
    launches = [frame for _, frame in frames if isinstance(frame, HostLaunchRunnerFrame)]
    assert len(launches) == 1 and launches[0].workspace == expected_workspace
    grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(created.id)
    assert [grant.user_id for grant in grants] == ["alice"]

    assert CREATED_BY_SESSION_LABEL_KEY not in store.get_conversation(manager["id"]).labels
    # Discovery and label-write rules are independent of the routing variants above.
    if child_manager or move_before_recheck or policy_denied or source_viewers:
        return

    info = json.loads(
        await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": created_id}),
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    assert info["created_by_session_id"] == manager["id"]
    listing = json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=auth_client,
            conversation_id=manager["id"],
        )
    )
    worker = next(row for row in listing["sessions"] if row["session_id"] == created_id)
    assert worker["created_by_session_id"] == manager["id"]
    assert worker["parent_session_id"] is None
    for value in ("", source["id"]):
        labels = {CREATED_BY_SESSION_LABEL_KEY: value}
        response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": labels})
        assert response.status_code == 400, response.text
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": labels}
        )
        assert response.status_code == 400, response.text
    response = await auth_client.patch(f"/v1/sessions/{created_id}", json={"labels": {}})
    assert response.status_code == 200, response.text
    assert (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(created_id)
        .labels[CREATED_BY_SESSION_LABEL_KEY]
        == manager["id"]
    )


@pytest.mark.parametrize("shape", ["parent", "multipart", "initial_items", "skill"])
async def test_independent_creation_rejects_child_or_bundle_shape(auth_client, peers, shape):
    from tests._helpers.session import post_session_bundle

    source, _, _ = peers
    headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    if shape == "parent":
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        expected = "Orchestration creates independent sessions"
    elif shape in ("initial_items", "skill"):
        first = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Unattributed task"}],
            },
        }
        payload = (
            {"initial_items": [first]}
            if shape == "initial_items"
            else {
                "initial_input": {
                    "type": "slash_command",
                    "data": {"kind": "skill", "name": "work"},
                }
            }
        )
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers=headers,
            json={"agent_id": source["agent_id"], "host_type": "managed", **payload},
        )
        expected = "Use initial_input" if shape == "initial_items" else "first task must be text"
    else:
        response = await post_session_bundle(
            auth_client.post, "/v1/sessions", b"unused", metadata={}, headers=headers
        )
        expected = "Independent sessions require agent_id"
    assert response.status_code == 400, response.text
    assert expected in response.text


@pytest.mark.parametrize("provider", ["modal", "daytona", "embedding"])
async def test_managed_creation_uses_default_provider_and_saves_private_first_task(
    auth_client, auth_app, peers, db_uri, monkeypatch, provider
):
    import asyncio
    from dataclasses import replace

    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions

    source, _, _ = peers
    deployment = parse_sandbox_config(
        {
            "server_url": "https://managed-test.example.com",
            "providers": [
                {"provider": "lakebox"},
                {"provider": "modal" if provider == "embedding" else provider},
            ],
        }
    )
    assert deployment is not None
    if provider == "embedding":
        deployment = replace(deployment, configs=(replace(deployment.default, provider=provider),))
    auth_app.state.sandbox_config = deployment
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    launched = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()
        await release.wait()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    try:
        with capture_debug_rows("server") as rows:
            result = json.loads(
                await execute_tool(
                    tool_name="sys_session_create",
                    arguments=json.dumps(
                        {
                            "agent_id": source["agent_id"],
                            "placement": "managed",
                            "message": "First task",
                        }
                    ),
                    server_client=auth_client,
                    conversation_id=source["id"],
                )
            )
        assert "error" not in result, result
        await asyncio.wait_for(launched.wait(), timeout=2)
        session_id = result["conversation_id"]
        created = next(row for row in rows if row["event_name"] == "session_created")
        assert created["session_id"] == session_id
        assert created["attributes"]["created_by_session_id"] == source["id"]
        assert "parent_session_id" not in created["attributes"]
        assert captured["provider"] is None
        assert captured["sandbox_config"].for_provider(None).provider == provider
        assert captured["owner"] == "alice"
        store = SqlAlchemyConversationStore(db_uri)
        assert store.get_conversation(session_id).parent_conversation_id is None
        assert (
            store.get_conversation(session_id).labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
        )
        assert "First task" in str(store.list_pending_inputs(session_id))
        assert f"[Message from session {source['id']}]" in str(
            store.list_pending_inputs(session_id)
        )
        grants, _ = SqlAlchemyPermissionStore(db_uri).list_for_session(session_id)
        assert [g.user_id for g in grants] == ["alice"]
    finally:
        release.set()
        await auth_app.state.startup_inputs.shutdown()


@pytest.mark.parametrize("configured", [False, True])
async def test_managed_creation_requires_launchable_provider(
    auth_client, auth_app, peers, monkeypatch, configured
):
    from dataclasses import replace

    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers
    deployment = auth_app.state.sandbox_config
    auth_app.state.sandbox_config = (
        replace(deployment, configs=(replace(deployment.default, managed_launch_supported=False),))
        if configured
        else None
    )

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Unavailable managed creation must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    result = json.loads(
        await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": source["agent_id"], "placement": "managed"}),
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    assert "Managed sandboxes are unavailable" in result["detail"]


async def test_orchestrated_endpoint_requires_source(auth_client, peers, monkeypatch):
    from omnigent.server.routes.sessions import routes_core

    source, _, _ = peers

    async def unexpected_creation(*args, **kwargs):
        pytest.fail("Missing source must not create a session")

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", unexpected_creation)
    response = await auth_client.post(
        "/v1/sessions/orchestrated", json={"agent_id": source["agent_id"]}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "restriction,before_admission",
    [("sharing", False), ("sharing", True), ("admin_messaging", False)],
)
async def test_startup_queue_admission_and_replay(
    auth_client, auth_app, peers, db_uri, monkeypatch, restriction, before_admission
):
    from omnigent.server.startup_inputs import STARTUP_LABEL

    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(target["id"], {STARTUP_LABEL: "pending"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    startup = auth_app.state.startup_inputs
    accepts = startup.accepts
    refusal = (
        "shared with edit access cannot"
        if restriction == "sharing"
        else "Messaging other sessions is disabled"
    )

    def restrict_source():
        if restriction == "sharing":
            permissions.grant("bob", source["id"], LEVEL_EDIT)
        else:
            write_peer_session_messaging_override(False)

    async def check_startup(*args):
        result = await accepts(*args)
        assert result is True
        if before_admission:
            restrict_source()
        return result

    monkeypatch.setattr(startup, "accepts", check_startup)
    result = await _send(auth_client, source, target)
    pending = store.list_pending_inputs(target["id"])
    assert not captured.get("posts")
    if before_admission:
        assert refusal in result["detail"]
        assert pending == []
    else:
        assert result["queued"] is True
        assert len(pending) == 1
        restrict_source()
        await startup._deliver(auth_app, target["id"], pending[0])
        await startup._deliver(auth_app, target["id"], pending[0])
        delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
        assert len(delivered) == 1
        assert f"[Message from session {source['id']}, message msg_" in json.dumps(delivered)
        assert store.list_pending_inputs(target["id"]) == []
        assert refusal in (await _send(auth_client, source, target))["detail"]


async def test_an_fyi_to_a_session_without_a_durable_inbox_reports_action(
    auth_client, peers, db_uri
):
    source, target, captured = peers
    result = await _send(auth_client, source, target, kind="fyi")
    assert result["delivered_kind"] == "action"
    assert result["delivered_kind_reason"] == "no durable inbox"
    assert SqlAlchemyConversationStore(db_uri).list_pending_inputs(target["id"]) == []
    assert [path for path, _ in captured.get("posts", []) if path.endswith("/events")]


@pytest.mark.parametrize(
    "managed_sandbox",
    [True, pytest.param(False, marks=pytest.mark.no_managed_sandbox)],
    ids=["managed", "local_only"],
)
@pytest.mark.parametrize("kind", ["action", "fyi"])
@pytest.mark.parametrize("busy", [True, False])
async def test_peer_message_to_a_busy_session_waits_in_the_durable_queue(
    auth_client, auth_app, peers, db_uri, monkeypatch, busy, kind, managed_sandbox
):
    """An action waits only while the receiver is busy; an fyi always waits for its next turn."""
    from unittest.mock import AsyncMock

    from omnigent.host.frames import HostHelloFrame

    source, target, captured = peers
    assert (auth_app.state.sandbox_config is not None) is managed_sandbox
    store = SqlAlchemyConversationStore(db_uri)
    host_id = uuid4().hex
    store.set_host_id(target["id"], host_id, "/tmp/inbox-workspace")
    auth_app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="inbox", runners=[]),
        owner=None,
    )
    store.set_session_live_status(target["id"], "running" if busy else "idle")
    startup = auth_app.state.startup_inputs
    monkeypatch.setattr(startup, "host_changed", lambda *_: None)

    result = await _send(auth_client, source, target, kind=kind)
    pending = store.list_pending_inputs(target["id"])
    # Every ack names its kind; only a downgraded fyi carries a reason.
    assert result["delivered_kind"] == kind
    assert "delivered_kind_reason" not in result
    if not busy and kind == "action":
        assert pending == []
        return
    assert result["queued"] is True
    assert result["pending_id"] and result["message_id"].startswith("msg_")
    assert not [path for path, _ in captured.get("posts", []) if path.endswith("/events")]
    [row] = pending
    assert row.data.startup_options.address == target["id"]
    if kind == "fyi":
        from omnigent.server import session_live_state
        from omnigent.server.routes._sessions.helpers import _publish_status

        assert row.data.startup_options.deliver_at_turn
        assert not await startup.has_undelivered(target["id"])
        # The server sees the receiver start a turn, with no harness hook involved.
        submit = session_live_state.submit
        monkeypatch.setattr(session_live_state, "conversation_store", lambda: store)
        monkeypatch.setattr(
            session_live_state,
            "submit",
            lambda name, fn, *args, **kw: (
                fn(*args) if name == "turn_inputs" else submit(name, fn, *args, **kw)
            ),
        )
        _publish_status(target["id"], "idle")
        _publish_status(target["id"], "running")
        [row] = store.list_pending_inputs(target["id"])
        assert not row.data.startup_options.deliver_at_turn
        assert await startup.has_undelivered(target["id"])
    await startup._deliver(auth_app, target["id"], row)
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1 and result["message_id"] in json.dumps(delivered)


@pytest.mark.no_managed_sandbox
async def test_fyi_messages_merge_on_a_hub_without_managed_sandboxes(
    auth_client, auth_app, peers, db_uri, monkeypatch
):
    from unittest.mock import AsyncMock

    from omnigent.host.frames import HostHelloFrame
    from omnigent.server import session_live_state
    import asyncio

    from omnigent.server.routes._sessions.helpers import _publish_status
    from omnigent.server.startup_inputs import STARTUP_LABEL

    source, target, captured = peers
    assert auth_app.state.sandbox_config is None
    store = SqlAlchemyConversationStore(db_uri)
    host_id = uuid4().hex
    store.set_host_id(target["id"], host_id, "/tmp/inbox-workspace")
    auth_app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="inbox", runners=[]),
        owner=None,
    )
    startup = auth_app.state.startup_inputs
    monkeypatch.setattr(startup, "host_changed", lambda *_: None)
    notes = ["measured, nothing to do", "rerun green"]
    for note in notes:
        result = await _send(auth_client, source, target, message=note, kind="fyi")
        assert result["delivered_kind"] == "fyi"
    assert len(store.list_pending_inputs(target["id"])) == 2
    info = (await auth_client.get("/v1/info")).json()
    assert info["durable_initial_input"] is False
    # Held fyi neither block a reset nor fall to an interrupt, even one that
    # drops an ordinary queued input next to them.
    assert await startup.has_droppable(target["id"]) is False
    store.set_session_live_status(target["id"], "running")
    await _send(auth_client, source, target, message="queued action")
    assert await startup.has_droppable(target["id"]) is True
    interrupted = await auth_client.post(
        f"/v1/sessions/{target['id']}/events", json={"type": "interrupt"}
    )
    assert interrupted.status_code < 300, interrupted.text
    assert len(store.list_pending_inputs(target["id"])) == 2
    assert await startup.has_droppable(target["id"]) is False
    assert store.get_conversation(target["id"]).labels.get(STARTUP_LABEL) != "cancelled"
    captured.get("posts", []).clear()

    submit = session_live_state.submit
    monkeypatch.setattr(session_live_state, "conversation_store", lambda: store)
    monkeypatch.setattr(
        session_live_state,
        "submit",
        lambda name, fn, *args, **kw: (
            fn(*args) if name == "turn_inputs" else submit(name, fn, *args, **kw)
        ),
    )
    _publish_status(target["id"], "idle")
    _publish_status(target["id"], "running")
    assert len(store.list_pending_inputs(target["id"])) == 1
    # The startup worker, not a direct call, picks up the released row.
    connection = auth_app.state.host_registry.get(host_id)
    assert await startup._recover_session(auth_app, target["id"], connection) is True
    await asyncio.gather(*startup._deliveries.values())
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1
    assert all(note in json.dumps(delivered) for note in notes)
    assert "queued action" not in json.dumps(delivered)


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("target_kind", ["root", "child", "side_chat"])
@pytest.mark.parametrize("legacy_host", [False, True])
async def test_peer_message_readdresses_across_replicas(
    two_replica_stack, db_uri, monkeypatch, target_kind, legacy_host
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions.helpers import _get_runner_client_impl
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    target = await _create_session_as(stack.tunnel_client, "", "alice")
    source_headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    _bind_runner(db_uri, target["id"])
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(target["id"], host_id, workspace="/worker")
    captured = _stub_late_runner_bind(monkeypatch)
    capture_client = sessions._get_runner_client

    async def resolve_runner(session_id, router, **kwargs):
        if router is stack.tunnel_replica.state.runner_router:
            return await capture_client(session_id, router, **kwargs)
        return await _get_runner_client_impl(session_id, router, **kwargs)

    monkeypatch.setattr(sessions, "_get_runner_client", resolve_runner)
    if target_kind == "child":
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers={"X-Forwarded-Email": "alice"},
            json={"agent_id": target["agent_id"], "parent_session_id": target["id"]},
        )
        assert response.status_code == 201, response.text
        target = response.json()
    elif target_kind == "side_chat":
        side = await _create_session_as(stack.tunnel_client, "", "alice")
        _bind_runner(db_uri, side["id"])
        stack.conv_store.set_labels(
            side["id"], {"omnigent.side_chat": "1", "omnigent.side_chat.source_id": target["id"]}
        )
        target = side
    captured.clear()
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    received = []
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if legacy_host else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        received.append((request.method, request.url.path, key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=source_headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = await _send(client, source, target)
        assert client.headers[OMNIGENT_SLICE_KEY_HEADER] == "sender-host"
    assert result.get("queued") is True, result
    posts = [(key, status) for method, path, key, status in received if method == "POST"]
    assert posts == (
        [("sender-host", 400), (host_id, 400), (None, 202)]
        if legacy_host
        else [("sender-host", 400), (host_id, 202)]
    )
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1
    assert f"[Message from session {source['id']}, message msg_" in json.dumps(delivered)


@pytest.mark.parametrize("two_replica_stack", ["managed"], indirect=True)
async def test_managed_create_is_addressed_before_provisioning(
    two_replica_stack, db_uri, monkeypatch
):
    import asyncio

    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes.sessions import routes_core

    stack = two_replica_stack
    source = await _create_session_as(stack.keyless_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    launched = asyncio.Event()
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        launched.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    reserved = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        if request.method == "POST":
            body = json.loads(request.content)
            assert key == body["host_id"] and key != "sender-host"
            reserved.append(key)
        response = await transports[int(key != "sender-host")].handle_async_request(request)
        await response.aread()
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "managed",
                        "message": "First task",
                    }
                ),
                server_client=client,
                conversation_id=source["id"],
            )
        )
    assert "error" not in result, result
    await asyncio.wait_for(launched.wait(), timeout=2)
    assert reserved == [captured["host_id"]]
    assert captured["host_registry"] is stack.tunnel_replica.state.host_registry
    worker = stack.conv_store.get_conversation(result["conversation_id"])
    assert worker.labels["omnigent.sandbox.requested_host_id"] == reserved[0]
    pending = stack.conv_store.list_pending_inputs(worker.id)
    assert len(pending) == 1
    assert (
        pending[0].data.content[0]["text"] == f"[Message from session {source['id']}]\nFirst task"
    )
    await stack.tunnel_replica.state.startup_inputs.shutdown()


@pytest.mark.parametrize("two_replica_stack", ["authenticated"], indirect=True)
@pytest.mark.parametrize("child_manager", [False, True])
@pytest.mark.parametrize("routing", ["host", "keyless", "wrong_replica", "disconnect", "offline"])
async def test_same_host_creation_across_replicas(
    two_replica_stack, db_uri, monkeypatch, child_manager, routing
):
    import httpx

    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.runner._entry import _RunnerDatabricksAuth
    from omnigent.server.routes import _host_launch
    from tests.server.integration.test_sessions_permissions import _register_online_host

    stack = two_replica_stack
    source = await _create_session_as(stack.tunnel_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    host_id = uuid4().hex
    _register_online_host(stack.tunnel_replica, host_id, "alice")
    stack.conv_store.set_host_id(source["id"], host_id, workspace="/worker")
    manager = source
    if child_manager:
        response = await stack.tunnel_client.post(
            "/v1/sessions",
            headers=headers,
            json={"agent_id": source["agent_id"], "parent_session_id": source["id"]},
        )
        assert response.status_code == 201, response.text
        manager = response.json()
        assert stack.conv_store.get_conversation(manager["id"]).host_id is None
    before = {s.id for s in stack.conv_store.list_conversations().data}
    launches = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
            if routing in {"disconnect", "offline"}:
                # Workspace validation succeeded; the tunnel vanishes before launch.
                monkeypatch.setattr(
                    stack.tunnel_replica.state.host_registry, "get", lambda _: None
                )
        elif isinstance(frame, HostLaunchRunnerFrame):
            launches.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(stack.tunnel_replica.state.host_registry, "send_text", answer_host)
    monkeypatch.setattr(_host_launch, "_host_sharded", routing != "offline")
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "0" if routing == "keyless" else "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", host_id)
    transports = [
        httpx.ASGITransport(app=app) for app in (stack.keyless_replica, stack.tunnel_replica)
    ]
    posts = []

    async def ingress(request):
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        on_target = key is None if routing in {"keyless", "wrong_replica"} else key == host_id
        response = await transports[int(on_target)].handle_async_request(request)
        await response.aread()
        if request.method == "POST":
            posts.append((key, response.status_code))
        return response

    async with open_server_client(
        "http://sharded",
        headers=headers,
        auth=_RunnerDatabricksAuth(lambda: "test-bearer", "http://sharded"),
        transport=httpx.MockTransport(ingress),
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": source["agent_id"],
                        "placement": "same_host",
                        "title": "Review worker",
                    }
                ),
                server_client=client,
                conversation_id=manager["id"],
            )
        )
        if routing in {"disconnect", "offline"}:
            listing = json.loads(
                await execute_tool(
                    tool_name="sys_session_list",
                    arguments="{}",
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            workers = [s for s in listing["sessions"] if s["title"] == result["title"]]
            assert len(workers) == 1
            recovered = json.loads(
                await execute_tool(
                    tool_name="sys_session_get_info",
                    arguments=json.dumps({"session_id": workers[0]["session_id"]}),
                    server_client=client,
                    conversation_id=manager["id"],
                )
            )
            assert recovered["agent_id"] == result["agent_id"]
            assert recovered["host_id"] == host_id
    created = {s.id for s in stack.conv_store.list_conversations().data} - before
    assert len(posts) == 1
    if routing in {"wrong_replica", "disconnect", "offline"}:
        assert "do not automatically retry" in result["error"], result
        assert posts == [(host_id, 409 if routing == "offline" else 400)]
        assert len(created) == (0 if routing == "wrong_replica" else 1)
        assert launches == []
    else:
        assert "error" not in result, result
        assert posts == [(None if routing == "keyless" else host_id, 201)]
        assert created == {result["conversation_id"]}
        worker = stack.conv_store.get_conversation(result["conversation_id"])
        assert worker.parent_conversation_id is None
        assert worker.host_id == host_id and worker.workspace == "/worker"
        assert len(launches) == 1 and launches[0].session_id == worker.id


async def test_role_aliases_address_peers_by_name(auth_client, peers, db_uri):
    source, target, _ = peers
    assert (
        await auth_client.put(f"/v1/sessions/{source['id']}/alias", json={"alias": "Architect"})
    ).status_code == 200
    named = await auth_client.put(f"/v1/sessions/{target['id']}/alias", json={"alias": "Product"})
    assert named.json()["handle"] == "/Product"
    other = await _create_session_as(auth_client, "", "alice")
    taken = await auth_client.put(f"/v1/sessions/{other['id']}/alias", json={"alias": "product"})
    assert taken.status_code == 409
    forged = await auth_client.patch(
        f"/v1/sessions/{other['id']}", json={"labels": {ALIAS_LABEL_KEY: "Product"}}
    )
    assert forged.status_code == 400

    assert (await _send(auth_client, source, {"id": "Product"})).get("queued") is True
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from /Architect (session {source['id']}, message msg_" in items.text

    store = SqlAlchemyConversationStore(db_uri)
    for session in (source, target):
        store.set_labels(session["id"], {PROJECT_LABEL_KEY: "py-qwant"})
    assert (await _send(auth_client, source, {"id": "py-qwant/Product"})).get("queued") is True
    items = await auth_client.get(f"/v1/sessions/{target['id']}/items")
    assert f"[Message from py-qwant/Architect (session {source['id']}, message msg_" in items.text
    assert (
        "Could not resolve"
        in (await _send(auth_client, source, {"id": "py-qwant/Nobody"}))["error"]
    )

    listing = json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=auth_client,
            conversation_id=source["id"],
        )
    )
    handles = {row["session_id"]: row["handle"] for row in listing["sessions"]}
    assert handles[target["id"]] == "py-qwant/Product"
    assert handles[other["id"]] is None

    listed = (await auth_client.get("/v1/sessions/aliases", params={"project": "py-qwant"})).json()
    assert {entry["handle"] for entry in listed["data"]} == {
        "py-qwant/Architect",
        "py-qwant/Product",
    }
    assert (await auth_client.delete(f"/v1/sessions/{target['id']}/alias")).status_code == 204
    assert (
        "Could not resolve"
        in (await _send(auth_client, source, {"id": "py-qwant/Product"}))["error"]
    )

    claimed = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "alias": "Bench"}
    )
    assert claimed.status_code in (200, 201), claimed.text
    resolved = await auth_client.get("/v1/sessions/aliases/resolve", params={"address": "Bench"})
    assert resolved.json()["session_id"] == claimed.json()["id"]
    bulk = await auth_client.post(
        "/v1/sessions/aliases/resolve",
        json={"addresses": ["Bench", "py-qwant/Architect", "py-qwant/Nobody"]},
    )
    rows = bulk.json()["data"]
    assert [row.get("session_id") for row in rows[:2]] == [claimed.json()["id"], source["id"]]
    assert "Nobody" in rows[2]["error"]
    before = len(store.list_conversations(limit=200).data)
    for alias in ("bench", "9bad"):
        refused = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "alias": alias}
        )
        assert refused.status_code in (400, 409), refused.text
    assert len(store.list_conversations(limit=200).data) == before

    # A stray label on a name with a row loses to the row, and the resolve repairs it.
    store.set_labels(other["id"], {ALIAS_LABEL_KEY: "bench"})
    resolved = await auth_client.get("/v1/sessions/aliases/resolve", params={"address": "Bench"})
    assert resolved.json()["session_id"] == claimed.json()["id"]
    assert ALIAS_LABEL_KEY not in store.get_conversation(other["id"]).labels

    # Unadopted labels have no row, so two live holders stay ambiguous; never pick one.
    store.set_labels(claimed.json()["id"], {ALIAS_LABEL_KEY: "Twin"})
    store.set_labels(other["id"], {ALIAS_LABEL_KEY: "twin"})
    ambiguous = await auth_client.get("/v1/sessions/aliases/resolve", params={"address": "Twin"})
    assert ambiguous.status_code == 409
    assert claimed.json()["id"] in ambiguous.text and other["id"] in ambiguous.text
    rows = (
        await auth_client.post(
            "/v1/sessions/aliases/resolve", json={"addresses": ["Twin", "py-qwant/Architect"]}
        )
    ).json()["data"]
    assert "ambiguous" in rows[0]["error"] and other["id"] in rows[0]["error"]
    assert rows[1]["session_id"] == source["id"]
    store.delete_label(claimed.json()["id"], ALIAS_LABEL_KEY)
    store.delete_label(other["id"], ALIAS_LABEL_KEY)

    # Namespace tree: nested projects, ?path=, move/cycle/delete rules, and path addressing.
    created = await auth_client.post("/v1/projects", json={"name": "ns"})
    assert created.status_code == 200, created.text
    parent = created.json()
    child = (
        await auth_client.post("/v1/projects", json={"name": "sub", "parent_id": parent["id"]})
    ).json()
    assert child["path"] == "ns/sub" and child["parent_id"] == parent["id"]
    assert (await auth_client.post("/v1/projects", json={"name": "a/b"})).status_code == 422
    found = (await auth_client.get("/v1/projects", params={"path": "NS/Sub"})).json()["data"]
    assert [row["id"] for row in found] == [child["id"]]
    assert (await auth_client.get("/v1/projects", params={"path": "ns/nope"})).json()["data"] == []
    cycle = await auth_client.patch(
        f"/v1/projects/{parent['id']}", json={"parent_id": child["id"]}
    )
    assert cycle.status_code == 400
    assert (await auth_client.delete(f"/v1/projects/{parent['id']}")).status_code == 409

    store.set_conversation_project(source["id"], child["id"])
    nested = await auth_client.get(
        "/v1/sessions/aliases/resolve", params={"address": "ns/sub/Architect"}
    )
    assert nested.json()["session_id"] == source["id"] and nested.json()["handle"] == (
        "ns/sub/Architect"
    )
    moved = await auth_client.patch(f"/v1/projects/{child['id']}", json={"parent_id": None})
    assert moved.json()["path"] == "sub"
    gone = await auth_client.get(
        "/v1/sessions/aliases/resolve", params={"address": "ns/sub/Architect"}
    )
    assert gone.status_code == 404
    again = await auth_client.get(
        "/v1/sessions/aliases/resolve", params={"address": "sub/Architect"}
    )
    assert again.json()["session_id"] == source["id"]
    assert (await auth_client.delete(f"/v1/projects/{parent['id']}")).status_code == 200


async def test_role_rows_follow_session_lifecycle(auth_client, peers, db_uri):
    """Every exit from a live session releases the role; none of them retakes it."""
    source, target, _ = peers
    roles = SqlAlchemyRoleStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)

    async def take(session: dict, alias: str = "Holder") -> str:
        taken = await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        assert taken.status_code == 200, taken.text
        assert taken.json()["path"] == "" and taken.json()["permanent"] is False
        assert taken.json()["handle"] == f"/{alias}"
        return taken.json()["role_id"]

    async def assert_vacant(role_id: str, reason: str) -> None:
        role = roles.get(role_id, user_id="alice")
        assert role.holder_session_id is None
        assert roles.events(role_id)[-1].reason == reason
        missing = await auth_client.get(
            "/v1/sessions/aliases/resolve", params={"address": "/Holder"}
        )
        assert missing.status_code == 404
        assert "is vacant" in missing.text

    role_id = await take(target)
    labels = store.get_conversation(target["id"]).labels
    assert labels[ROLE_ID_LABEL_KEY] == role_id
    listed = await auth_client.get("/v1/sessions/aliases", params={"alias": "holder"})
    assert [
        (entry["role_id"], entry["path"], entry["permanent"], entry["handle"])
        for entry in listed.json()["data"]
    ] == [(role_id, "", False, "/Holder")]

    # Agents still cannot set or release aliases outside creation.
    refused = await auth_client.delete(
        f"/v1/sessions/{target['id']}/alias", headers={ORCHESTRATION_SOURCE_HEADER: source["id"]}
    )
    assert refused.status_code == 403
    assert roles.get(role_id, user_id="alice").holder_session_id == target["id"]

    # Close through the labels PATCH.
    closed = await auth_client.patch(
        f"/v1/sessions/{target['id']}", json={"labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}}
    )
    assert closed.status_code == 200, closed.text
    await assert_vacant(role_id, "close")
    assert ALIAS_LABEL_KEY not in store.get_conversation(target["id"]).labels

    # Archive releases; unarchive does not retake.
    holder = await _create_session_as(auth_client, "", "alice")
    assert await take(holder) == role_id
    archived = await auth_client.patch(f"/v1/sessions/{holder['id']}", json={"archived": True})
    assert archived.status_code == 200, archived.text
    await assert_vacant(role_id, "archive")
    assert (
        await auth_client.patch(f"/v1/sessions/{holder['id']}", json={"archived": False})
    ).status_code == 200
    await assert_vacant(role_id, "archive")
    assert ALIAS_LABEL_KEY not in store.get_conversation(holder["id"]).labels

    # An archive whose release never ran: unarchive still leaves the session unaddressed.
    assert await take(holder) == role_id
    store.update_conversation_with_changes(holder["id"], archived=True)
    assert (
        await auth_client.patch(f"/v1/sessions/{holder['id']}", json={"archived": False})
    ).status_code == 200
    await assert_vacant(role_id, "unarchive")
    assert ALIAS_LABEL_KEY not in store.get_conversation(holder["id"]).labels

    # Delete, and DELETE alias as an explicit release.
    assert await take(holder) == role_id
    assert (await auth_client.delete(f"/v1/sessions/{holder['id']}/alias")).status_code == 204
    await assert_vacant(role_id, "alias_cleared")
    assert await take(holder) == role_id
    assert (await auth_client.delete(f"/v1/sessions/{holder['id']}")).status_code in (200, 204)
    await assert_vacant(role_id, "delete")

    generations = [event.generation for event in roles.events(role_id)]
    assert generations == list(range(1, len(generations) + 1))


async def test_create_with_a_role_claims_after_the_session_exists(
    auth_client, peers, db_uri, monkeypatch
):
    """Create then claim: a lost claim closes the orphan and the caller gets a 409."""
    source, target, _ = peers
    roles = SqlAlchemyRoleStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    body = {"agent_id": source["agent_id"], "alias": "Claimed"}

    created = await auth_client.post("/v1/sessions", json=body)
    assert created.status_code in (200, 201), created.text
    role = roles.find("Claimed", user_id="alice", project_id=None)
    assert role.holder_session_id == created.json()["id"]
    assert store.get_conversation(created.json()["id"]).labels[ROLE_ID_LABEL_KEY] == role.id

    # The rival claims in the window between the check and the claim.
    real_check = session_aliases.check_alias_for_new_session
    projects = SqlAlchemyProjectStore(db_uri)

    def check_then_lose(*args, **kwargs):
        value = real_check(*args, **kwargs)
        session_aliases.set_session_alias(
            store,
            projects,
            session=store.get_conversation(target["id"]),
            user_id="alice",
            alias=value,
        )
        return value

    before = {session.id for session in store.list_conversations(limit=200).data}
    with monkeypatch.context() as patch:
        patch.setattr(session_aliases, "check_alias_for_new_session", check_then_lose)
        lost = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "alias": "Raced"}
        )
    assert lost.status_code == 409, lost.text
    assert "already used" in lost.text
    orphans = {
        session.id for session in store.list_conversations(limit=200, include_archived=True).data
    } - before
    assert len(orphans) == 1
    orphan = store.get_conversation(orphans.pop())
    assert orphan.archived is True
    assert orphan.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE
    assert ALIAS_LABEL_KEY not in orphan.labels
    assert roles.find("Raced", user_id="alice", project_id=None).holder_session_id == target["id"]

    # A create into a project claims in that project's namespace.
    project = (await auth_client.post("/v1/projects", json={"name": "home"})).json()
    filed = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "alias": "Claimed", "project_id": project["id"]},
    )
    assert filed.status_code in (200, 201), filed.text
    assert (
        roles.find("Claimed", user_id="alice", project_id=project["id"]).holder_session_id
        == filed.json()["id"]
    )

    # A held name is refused before anything is created.
    count = len(store.list_conversations(limit=200, include_archived=True).data)
    held = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "alias": "claimed"}
    )
    assert held.status_code == 409
    assert len(store.list_conversations(limit=200, include_archived=True).data) == count

    # An overlay needs a role to attach to, and is never a path.
    orphaned = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "overlay_ref": "Bench"}
    )
    assert orphaned.status_code == 400
    traversal = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "alias": "Instance", "overlay_ref": "../Bench"},
    )
    assert traversal.status_code == 400

    instance = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "alias": "Instance", "overlay_ref": "Bench"},
    )
    assert instance.status_code in (200, 201), instance.text
    labels = store.get_conversation(instance.json()["id"]).labels
    assert labels[ROLE_OVERLAY_LABEL_KEY] == "Bench"
    assert roles.find("Instance", user_id="alice", project_id=None).overlay_ref == "Bench"

    # The mirror labels are server-owned on every client path.
    for key in (ROLE_ID_LABEL_KEY, ROLE_OVERLAY_LABEL_KEY):
        forged = await auth_client.patch(
            f"/v1/sessions/{target['id']}", json={"labels": {key: "Bench"}}
        )
        assert forged.status_code == 400, key
        seeded = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], "labels": {key: "Bench"}}
        )
        assert seeded.status_code == 400, key

    # A fork is a new session: it holds nothing.
    forked = store.fork_conversation(instance.json()["id"])
    assert not {ALIAS_LABEL_KEY, ROLE_ID_LABEL_KEY, ROLE_OVERLAY_LABEL_KEY} & set(
        store.get_conversation(forked.id).labels
    )


async def test_sys_role_list_shows_held_and_vacant_roles(auth_client, peers, db_uri):
    source, target, _ = peers
    created = await auth_client.post("/v1/projects", json={"name": "lab"})
    project = created.json()
    store = SqlAlchemyConversationStore(db_uri)
    for session in (source, target):
        assert store.set_conversation_project(session["id"], project["id"])
    assert (
        await auth_client.put(f"/v1/sessions/{target['id']}/alias", json={"alias": "Held"})
    ).status_code == 200
    assert (
        await auth_client.put(f"/v1/sessions/{source['id']}/alias", json={"alias": "Vacant"})
    ).status_code == 200
    assert (await auth_client.delete(f"/v1/sessions/{source['id']}/alias")).status_code == 204
    unadopted = await _create_session_as(auth_client, "", "alice")
    assert store.set_conversation_project(unadopted["id"], project["id"])
    store.set_labels(unadopted["id"], {ALIAS_LABEL_KEY: "Legacy"})
    root = await _create_session_as(auth_client, "", "alice")
    assert (
        await auth_client.put(f"/v1/sessions/{root['id']}/alias", json={"alias": "Scratch"})
    ).status_code == 200

    async def role_list(**arguments: str) -> dict:
        return json.loads(
            await execute_tool(
                tool_name="sys_role_list",
                arguments=json.dumps(arguments),
                server_client=auth_client,
                conversation_id=source["id"],
            )
        )

    listed = await role_list()
    assert listed["namespace"] == "lab"
    assert [
        (role["handle"], role["state"], role["holder_session_id"], role["role_id"] is not None)
        for role in listed["roles"]
    ] == [
        ("lab/Held", "held", target["id"], True),
        ("lab/Legacy", "held", unadopted["id"], False),
        ("lab/Vacant", "vacant", None, True),
    ]

    at_root = await role_list(namespace="/")
    assert at_root["namespace"] == "/"
    assert [role["handle"] for role in at_root["roles"]] == ["/Scratch"]
    assert (await role_list(namespace="nowhere"))["roles"] == []


async def test_bare_role_uses_the_namespace_the_caller_passes(auth_client, peers, db_uri):
    """U1 over HTTP: a picker's view and the CLI's working directory."""
    _, target, _ = peers
    project = (
        await auth_client.post(
            "/v1/projects", json={"name": "picked", "config": {"workspace": "/srv/picked"}}
        )
    ).json()
    store = SqlAlchemyConversationStore(db_uri)
    assert store.set_conversation_project(target["id"], project["id"])
    assert (
        await auth_client.put(f"/v1/sessions/{target['id']}/alias", json={"alias": "Picked"})
    ).status_code == 200
    resolve = "/v1/sessions/aliases/resolve"

    assert (await auth_client.get(resolve, params={"address": "Picked"})).status_code == 404
    for scope in ({"namespace": "picked"}, {"workspace": "/srv/picked/sub"}):
        one = await auth_client.get(resolve, params={"address": "Picked", **scope})
        assert one.json()["session_id"] == target["id"]
        assert one.json()["handle"] == "picked/Picked"
        bulk = await auth_client.post(resolve, json={"addresses": ["Picked"], **scope})
        assert bulk.json()["data"][0]["session_id"] == target["id"]


async def test_orchestrated_session_on_named_host_with_alias(auth_client, auth_app, peers, db_uri):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, _ = peers
    mine, theirs = uuid4().hex, uuid4().hex
    for host_id, owner in ((mine, "alice"), (theirs, "bob")):
        _register_online_host(auth_app, host_id, owner)
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, owner)
    store = SqlAlchemyConversationStore(db_uri)
    frames = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        frames.append((connection.host_id, frame))
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})

    auth_app.state.host_registry.send_text = answer_host
    store.set_labels(source["id"], {PROJECT_LABEL_KEY: "py-qwant"})
    headers = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    body = {"agent_id": source["agent_id"], "alias": "Reviewer", "workspace": "/srv/work"}

    before = {s.id for s in store.list_conversations().data}
    refused = await auth_client.post(
        "/v1/sessions/orchestrated", headers=headers, json={**body, "host_id": theirs}
    )
    assert refused.status_code == 403, refused.text
    assert {s.id for s in store.list_conversations().data} == before

    created = await auth_client.post(
        "/v1/sessions/orchestrated", headers=headers, json={**body, "host_id": mine}
    )
    assert created.status_code == 201, created.text
    row = store.get_conversation(created.json()["id"])
    assert row is not None
    assert (row.host_id, row.workspace) == (mine, "/srv/work")
    assert row.parent_conversation_id is None
    assert row.labels[CREATED_BY_SESSION_LABEL_KEY] == source["id"]
    assert row.labels[ALIAS_LABEL_KEY] == "Reviewer"
    assert row.labels[PROJECT_LABEL_KEY] == "py-qwant"
    assert {host for host, _ in frames} == {mine}
    resolved = await _send(auth_client, source, {"id": "Reviewer"})
    assert "Could not resolve" not in str(resolved.get("error", "")), resolved

    taken = await auth_client.post(
        "/v1/sessions/orchestrated", headers=headers, json={**body, "host_id": mine}
    )
    assert taken.status_code == 409, taken.text
    # Any orchestrated top-level create joins its creator's project, alias or not.
    plain = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers=headers,
        json={"agent_id": source["agent_id"], "workspace": "/srv/work", "host_id": mine},
    )
    assert plain.status_code == 201, plain.text
    plain_row = store.get_conversation(plain.json()["id"])
    assert plain_row is not None and plain_row.labels[PROJECT_LABEL_KEY] == "py-qwant"
    assert ALIAS_LABEL_KEY not in plain_row.labels
    no_workspace = await auth_client.post(
        "/v1/sessions/orchestrated",
        headers=headers,
        json={"agent_id": source["agent_id"], "host_id": mine},
    )
    assert no_workspace.status_code == 400, no_workspace.text


async def test_create_with_executor_fills_unset_fields_and_labels(
    auth_client, peers, db_uri, tmp_path, monkeypatch
):
    source, _, _ = peers
    monkeypatch.setenv("HOME", str(tmp_path))
    profiles = tmp_path / ".omnigent" / "executors.yaml"
    profiles.parent.mkdir()
    profiles.write_text("deep: {model: profile-model, reasoning_effort: high}\n")
    store = SqlAlchemyConversationStore(db_uri)

    unknown = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "executor": "nope"}
    )
    assert unknown.status_code == 400 and "deep" in unknown.text

    filled = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "executor": "deep"}
    )
    assert filled.status_code in (200, 201), filled.text
    row = store.get_conversation(filled.json()["id"])
    assert row is not None
    assert (row.model_override, row.reasoning_effort) == ("profile-model", "high")
    assert row.labels[EXECUTOR_LABEL_KEY] == "deep"

    explicit = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "executor": "deep", "model_override": "mine"},
    )
    assert explicit.status_code in (200, 201), explicit.text
    row = store.get_conversation(explicit.json()["id"])
    assert row is not None
    assert (row.model_override, row.reasoning_effort) == ("mine", "high")

    profiles.write_text("Judge_exec: {reasoning_effort: low}\n")
    role = tmp_path / ".omnigent" / "roles"
    role.mkdir()
    (role / "Judge.md").write_text("---\nexecutor: Judge_exec\n---\nJudge.\n")
    by_role = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "alias": "Judge"}
    )
    assert by_role.status_code in (200, 201), by_role.text
    row = store.get_conversation(by_role.json()["id"])
    assert row is not None and row.reasoning_effort == "low"
    assert row.labels[EXECUTOR_LABEL_KEY] == "Judge_exec"


async def test_create_executor_supplies_agent_id_and_label_is_reserved(
    auth_client, peers, db_uri, tmp_path, monkeypatch
):
    source, _, _ = peers
    monkeypatch.setenv("HOME", str(tmp_path))
    profiles = tmp_path / ".omnigent" / "executors.yaml"
    profiles.parent.mkdir()
    profiles.write_text(f"bundled: {{agent_id: {source['agent_id']}, model: m1}}\n")
    created = await auth_client.post("/v1/sessions", json={"executor": "bundled"})
    assert created.status_code in (200, 201), created.text
    row = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert row is not None and row.model_override == "m1"

    forged = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "labels": {EXECUTOR_LABEL_KEY: "bundled"}},
    )
    assert forged.status_code == 400, forged.text


# ── sys_role_take / sys_role_release: server-enforced owner approval ─────


@pytest.fixture
def owner_verdicts(monkeypatch):
    """Stand in for the owner's answer; records every question the server parks on."""
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.schemas import ElicitationResult

    state = {"action": "accept", "asked": []}

    async def answer(request, *, params, **kwargs):
        state["asked"].append((kwargs["session_id"], params.message))
        if state["action"] == "timeout":
            return None
        return ElicitationResult(action=state["action"])

    monkeypatch.setattr(routes_core, "_publish_and_wait_for_harness_elicitation", answer)
    return state


async def _role_tool(client, session, name, **arguments):
    return json.loads(
        await execute_tool(
            tool_name=name,
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id=session["id"],
        )
    )


def _alias_of(db_uri, session):
    return (
        SqlAlchemyConversationStore(db_uri)
        .get_conversation(session["id"])
        .labels.get(ALIAS_LABEL_KEY)
    )


async def test_role_take_and_release_apply_after_one_approval_each(
    auth_client, peers, db_uri, owner_verdicts
):
    source, _, _ = peers
    taken = await _role_tool(auth_client, source, "sys_role_take", role="Architect")
    assert taken == {"taken": True, "role": "Architect"}
    assert _alias_of(db_uri, source) == "Architect"
    (asked_session, question), *rest = owner_verdicts["asked"]
    assert not rest and asked_session == source["id"]
    assert source["id"] in question and "'/Architect'" in question

    assert await _role_tool(auth_client, source, "sys_role_release") == {"released": True}
    assert _alias_of(db_uri, source) is None
    assert len(owner_verdicts["asked"]) == 2  # approval is per call, not remembered


@pytest.mark.parametrize("action", ["decline", "cancel", "timeout"])
async def test_role_take_and_release_change_nothing_without_approval(
    auth_client, peers, db_uri, owner_verdicts, action
):
    source, _, _ = peers
    owner_verdicts["action"] = "accept"
    await _role_tool(auth_client, source, "sys_role_take", role="Keeper")
    owner_verdicts["action"] = action

    refused_release = await _role_tool(auth_client, source, "sys_role_release")
    assert refused_release["error"].endswith("returned 403")
    assert "did not approve" in refused_release["detail"]
    assert _alias_of(db_uri, source) == "Keeper"

    owner_verdicts["action"] = "accept"
    await _role_tool(auth_client, source, "sys_role_release")
    owner_verdicts["action"] = action
    refused_take = await _role_tool(auth_client, source, "sys_role_take", role="Other")
    assert refused_take["error"].endswith("returned 403")
    assert _alias_of(db_uri, source) is None


async def test_role_take_conflicts_and_one_role_rules_hold_after_approval(
    auth_client, peers, db_uri, owner_verdicts
):
    source, target, _ = peers
    assert (
        await auth_client.put(f"/v1/sessions/{target['id']}/alias", json={"alias": "Held"})
    ).status_code == 200
    held = await _role_tool(auth_client, source, "sys_role_take", role="Held")
    assert held["error"].endswith("returned 409") and _alias_of(db_uri, source) is None

    assert (await _role_tool(auth_client, source, "sys_role_take", role="First"))["taken"]
    second = await _role_tool(auth_client, source, "sys_role_take", role="Second")
    assert second["error"].endswith("returned 409") and "release it first" in second["detail"]
    assert _alias_of(db_uri, source) == "First"


async def test_role_release_without_a_role_is_refused_before_asking(
    auth_client, peers, owner_verdicts
):
    source, _, _ = peers
    result = await _role_tool(auth_client, source, "sys_role_release")
    assert result["error"].endswith("returned 400") and "holds no role" in result["detail"]
    assert owner_verdicts["asked"] == []


async def test_forged_agent_call_cannot_skip_approval_or_touch_another_session(
    auth_client, peers, db_uri, owner_verdicts
):
    source, target, _ = peers
    owner_verdicts["action"] = "decline"
    forged = {ORCHESTRATION_SOURCE_HEADER: source["id"]}
    own = await auth_client.put(
        f"/v1/sessions/{source['id']}/alias", json={"alias": "Sneaky"}, headers=forged
    )
    other = await auth_client.put(
        f"/v1/sessions/{target['id']}/alias", json={"alias": "Sneaky"}, headers=forged
    )
    assert (own.status_code, other.status_code) == (403, 403)
    assert _alias_of(db_uri, source) is None and _alias_of(db_uri, target) is None
    assert [asked[0] for asked in owner_verdicts["asked"]] == [source["id"]]


# ── sys_role_release(force=true): freeing a role another session holds ──


def _role_row(db_uri, name):
    roles = SqlAlchemyRoleStore(db_uri)
    role = roles.find(name, user_id="alice", project_id=None)
    return role, roles.events(role.id)


def _labels_of(db_uri, session):
    return SqlAlchemyConversationStore(db_uri).get_conversation(session["id"]).labels


def _mark_runner_online(db_uri, session):
    store = SqlAlchemyConversationStore(db_uri)
    runner_id = store.get_session_connectivity([session["id"]])[session["id"]].runner_id
    store.touch_runner_liveness([runner_id], int(time.time()))


async def _hold(client, session, alias):
    response = await client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("holder_online", [False, True])
async def test_force_release_asks_the_owner_then_frees_the_role(
    auth_client, peers, db_uri, owner_verdicts, holder_online
):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    _bind_runner(db_uri, holder["id"])  # bound but never seen: its runner is offline
    if holder_online:
        _mark_runner_online(db_uri, holder)
    before, _ = _role_row(db_uri, "Keeper")

    result = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert result["released"] is True and result["holder_session_id"] is None
    assert result["previous_holder_session_id"] == holder["id"]
    [(asked_session, question)] = owner_verdicts["asked"]
    assert asked_session == source["id"]
    assert "FORCE-RELEASE" in question and holder["id"] in question
    assert f"generation {before.generation}" in question
    assert ("runner online" if holder_online else "runner offline") in question

    after, events = _role_row(db_uri, "Keeper")
    assert after.holder_session_id is None
    assert (events[-1].kind, events[-1].actor, events[-1].reason) == (
        "release",
        "owner",
        f"forced:{source['id']}",
    )
    labels = _labels_of(db_uri, holder)
    assert ALIAS_LABEL_KEY not in labels and ROLE_ID_LABEL_KEY not in labels
    assert labels[ROLE_RELEASED_LABEL_KEY] == "/Keeper"
    assert labels[ROLE_RELEASED_BY_LABEL_KEY] == source["id"]
    assert labels[ROLE_RELEASED_TO_LABEL_KEY] == ""
    notice = role_release_instruction(labels)
    assert "/Keeper" in notice and source["id"] in notice and "vacant" in notice
    # Only a holder whose runner is online is sent the notice; a dead runner is not woken.
    assert result["holder_notified"] is holder_online and result["notice_failed"] is False
    items = (await auth_client.get(f"/v1/sessions/{holder['id']}/items")).text
    assert ("force-released" in items) is holder_online

    # Sends to the role fail clearly, naming the last holder and how to take it.
    vacant = await _send(auth_client, source, {"id": "Keeper"})
    assert holder["id"] in json.dumps(vacant) and "sys_role_take" in json.dumps(vacant)


@pytest.mark.parametrize("action", ["decline", "timeout"])
async def test_force_release_without_approval_changes_nothing(
    auth_client, peers, db_uri, owner_verdicts, action
):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    before, before_events = _role_row(db_uri, "Keeper")
    labels_before = _labels_of(db_uri, holder)
    owner_verdicts["action"] = action

    refused = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert refused["error"].endswith("returned 403")
    after, after_events = _role_row(db_uri, "Keeper")
    assert (after.holder_session_id, after.generation) == (holder["id"], before.generation)
    assert len(after_events) == len(before_events)
    assert _labels_of(db_uri, holder) == labels_before


async def test_force_release_loses_to_a_change_made_while_asking(
    auth_client, peers, db_uri, owner_verdicts, monkeypatch
):
    from omnigent.server.routes.sessions import routes_core

    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    answer = routes_core._publish_and_wait_for_harness_elicitation

    async def retake_while_asking(request, **kwargs):
        # The holder releases and takes the role back before the owner answers.
        assert (await auth_client.delete(f"/v1/sessions/{holder['id']}/alias")).status_code == 204
        await _hold(auth_client, holder, "Keeper")
        return await answer(request, **kwargs)

    monkeypatch.setattr(
        routes_core, "_publish_and_wait_for_harness_elicitation", retake_while_asking
    )
    lost = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert lost["error"].endswith("returned 409") and "nothing was changed" in lost["detail"]
    after, _ = _role_row(db_uri, "Keeper")
    assert after.holder_session_id == holder["id"]
    labels = _labels_of(db_uri, holder)
    assert labels[ALIAS_LABEL_KEY] == "Keeper" and ROLE_RELEASED_LABEL_KEY not in labels


async def test_force_release_can_hand_the_role_to_another_session(
    auth_client, peers, db_uri, owner_verdicts
):
    source, holder, _ = peers
    successor = await _create_session_as(auth_client, "", "alice")
    await _hold(auth_client, holder, "Keeper")
    await _hold(auth_client, source, "Other")

    busy = await _role_tool(
        auth_client, source, "sys_role_release", role="Keeper", force=True, give_to=source["id"]
    )
    assert busy["error"].endswith("returned 400") and "already holds" in busy["detail"]
    result = await _role_tool(
        auth_client, source, "sys_role_release", role="Keeper", force=True, give_to=successor["id"]
    )
    assert result["holder_session_id"] == successor["id"]
    assert "give it to session" in owner_verdicts["asked"][-1][1]

    after, events = _role_row(db_uri, "Keeper")
    assert after.holder_session_id == successor["id"]
    assert (events[-1].kind, events[-1].prev_session_id, events[-1].reason) == (
        "takeover",
        holder["id"],
        f"forced:{source['id']}",
    )
    assert _alias_of(db_uri, successor) == "Keeper" and _alias_of(db_uri, holder) is None
    assert _labels_of(db_uri, holder)[ROLE_RELEASED_TO_LABEL_KEY] == successor["id"]
    # Taking a role again clears the old notice.
    await _hold(auth_client, holder, "Fresh")
    assert ROLE_RELEASED_LABEL_KEY not in _labels_of(db_uri, holder)


async def test_force_released_holder_can_no_longer_speak_for_the_role_in_threads(
    auth_client, peers, db_uri, owner_verdicts
):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    _bind_runner(db_uri, holder["id"])
    _mark_runner_online(db_uri, holder)
    created = await auth_client.post(
        "/v1/sessions/group-threads",
        json={"title": "Plan", "members": [{"kind": "role", "ref": "/Keeper"}]},
    )
    assert created.status_code == 201, created.text
    key = created.json()["thread"]["key"]
    assert "post" in await _tool(auth_client, holder, "sys_thread_post", thread=key, message="hi")

    await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    refused = await _tool(auth_client, holder, "sys_thread_post", thread=key, message="still?")
    assert "not a member" in refused["detail"]
    # Its own handle no longer resolves to it either.
    vacant = await _send(auth_client, holder, {"id": "Keeper"})
    assert "vacant" in json.dumps(vacant)


async def test_a_failed_force_release_leaves_the_holder_labelled(
    auth_client, peers, db_uri, owner_verdicts, monkeypatch
):
    from omnigent.stores.role_store.sqlalchemy_store import SqlAlchemyRoleStore

    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")

    def broken_release(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(SqlAlchemyRoleStore, "release", broken_release)
    failed = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert "error" in failed
    assert _alias_of(db_uri, holder) == "Keeper"
    assert _role_row(db_uri, "Keeper")[0].holder_session_id == holder["id"]


async def test_a_failed_notice_still_reports_the_release(
    auth_client, peers, db_uri, owner_verdicts, monkeypatch
):
    from omnigent.runtime import prompt

    def broken_notice(labels):
        raise RuntimeError("notice unavailable")

    monkeypatch.setattr(prompt, "role_release_instruction", broken_notice)
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    _bind_runner(db_uri, holder["id"])
    _mark_runner_online(db_uri, holder)
    result = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert result["released"] is True and result["notice_failed"] is True
    assert result["holder_notified"] is False
    assert _role_row(db_uri, "Keeper")[0].holder_session_id is None


async def test_owner_force_release_is_its_own_approval(auth_client, peers, db_uri, owner_verdicts):
    _, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    unforced = await auth_client.post("/v1/sessions/aliases/roles/Keeper/release", json={})
    assert unforced.status_code == 400
    response = await auth_client.post(
        "/v1/sessions/aliases/roles/Keeper/release", json={"force": True}
    )
    assert response.status_code == 200, response.text
    assert owner_verdicts["asked"] == []
    _, events = _role_row(db_uri, "Keeper")
    assert (events[-1].actor, events[-1].reason) == ("owner", "forced:")
    assert _labels_of(db_uri, holder)[ROLE_RELEASED_BY_LABEL_KEY] == ""


async def test_force_release_never_consults_the_messaging_policy(
    auth_client, peers, db_uri, owner_verdicts, monkeypatch
):
    from omnigent.policies.builtins import messaging
    from omnigent.server.routes.sessions import routes_events

    consulted = []

    def allow_all(*args, **kwargs):
        consulted.append(args)

    monkeypatch.setattr(routes_events, "_evaluate_input_policy", allow_all)
    monkeypatch.setattr(messaging.MessagingRules, "decide", allow_all)
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    owner_verdicts["action"] = "decline"
    refused = await _role_tool(auth_client, source, "sys_role_release", role="Keeper", force=True)
    assert refused["error"].endswith("returned 403")
    assert len(owner_verdicts["asked"]) == 1 and consulted == []


async def test_held_role_conflict_names_the_holder_and_ways_out(
    auth_client, peers, db_uri, owner_verdicts
):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    _bind_runner(db_uri, holder["id"])
    conflict = await auth_client.put(
        f"/v1/sessions/{source['id']}/alias", json={"alias": "Keeper"}
    )
    assert conflict.status_code == 409
    for part in (holder["id"], "runner offline", "revive", "force=true"):
        assert part in conflict.text
    via_tool = await _role_tool(auth_client, source, "sys_role_take", role="Keeper")
    assert "sys_role_release(role='Keeper', force=true)" in via_tool["detail"]


@pytest.mark.parametrize("holder_online", [False, True])
async def test_agent_take_of_a_held_role_fails_before_asking_the_owner(
    auth_client, peers, db_uri, owner_verdicts, holder_online
):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    _bind_runner(db_uri, holder["id"])
    if holder_online:
        _mark_runner_online(db_uri, holder)
    held = await _role_tool(auth_client, source, "sys_role_take", role="Keeper")
    assert held["error"].endswith("returned 409")
    state = "runner online" if holder_online else "runner offline"
    for part in (holder["id"], state, "force=true"):
        assert part in held["detail"]
    assert owner_verdicts["asked"] == [] and _alias_of(db_uri, source) is None


@pytest.mark.parametrize(
    ("caller", "status"), [("child", 400), ("closed", 403), ("archived", 403)]
)
async def test_agent_take_from_an_ineligible_session_fails_before_asking_the_owner(
    auth_client, peers, db_uri, owner_verdicts, caller, status
):
    source, _, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    if caller == "child":
        parent = store.get_conversation(source["id"])
        child = store.create_conversation(
            agent_id=parent.agent_id, parent_conversation_id=source["id"]
        )
        auth_client.headers.update(_bind_runner(db_uri, child.id))
        source = {"id": child.id}
    elif caller == "closed":
        store.set_labels(source["id"], {"omnigent.closed": "true"})
    else:
        store.update_conversation_with_changes(source["id"], archived=True)
    refused = await _role_tool(auth_client, source, "sys_role_take", role="Keeper")
    assert refused["error"].endswith(f"returned {status}"), refused
    assert owner_verdicts["asked"] == [] and _alias_of(db_uri, source) is None


async def test_sys_role_list_reports_holder_runner_state(auth_client, peers, db_uri):
    source, holder, _ = peers
    await _hold(auth_client, holder, "Keeper")
    await _hold(auth_client, source, "Gone")
    assert (await auth_client.delete(f"/v1/sessions/{source['id']}/alias")).status_code == 204
    _bind_runner(db_uri, holder["id"])
    _mark_runner_online(db_uri, holder)
    listed = await _role_tool(auth_client, source, "sys_role_list")
    rows = {row["name"]: row for row in listed["roles"]}
    assert rows["Keeper"]["runner_online"] is True
    assert isinstance(rows["Keeper"]["runner_last_seen"], int)
    assert isinstance(rows["Keeper"]["last_activity_at"], int)
    assert (rows["Gone"]["runner_online"], rows["Gone"]["last_activity_at"]) == (None, None)


async def _tool(client, session, name, **arguments):
    return json.loads(
        await execute_tool(
            tool_name=name,
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id=session["id"],
        )
    )


async def test_owner_posts_into_a_role_thread(auth_client, peers, db_uri):
    architect, product, _ = peers
    _bind_runner(db_uri, product["id"])
    for session, alias in ((architect, "Architect"), (product, "Product")):
        assert (
            await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        ).status_code == 200
    assert (await _send(auth_client, architect, product, "Plan the API")).get("queued") is True
    await _tool(
        auth_client, product, "sys_thread_note", counterpart="/Architect", state="waiting_on_owner"
    )

    async def rows(session):
        threads = (await _tool(auth_client, session, "sys_thread_list"))["threads"]
        return {row["counterpart"]: row for row in threads}

    async def post(**body):
        return await auth_client.post(
            f"/v1/sessions/{product['id']}/threads/post",
            json={"counterpart": "/Architect", **body},
        )

    # A post to both sides is filed in the thread on each, and clears the wait on the owner.
    posted = await post(message="Use v2")
    assert posted.status_code == 202, posted.text
    assert posted.json()["delivered"] == [product["id"], architect["id"]]
    assert (await rows(product))["/Architect"]["state"] == "waiting_on_us"
    assert (await rows(architect))["/Product"]["state"] == "waiting_on_us"
    header = f"[Message from owner (message {posted.json()['message_id']})]"
    product_items = (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text
    architect_items = (await auth_client.get(f"/v1/sessions/{architect['id']}/items")).text
    assert f"{header} [thread /Architect]" in product_items
    assert f"{header} [thread /Product]" in architect_items

    # The thread view shows the post once, as the owner's.
    view = await auth_client.get(
        f"/v1/sessions/{product['id']}/threads/messages", params={"counterpart": "/Architect"}
    )
    assert [(m["direction"], m.get("to"), m["text"]) for m in view.json()["messages"]] == [
        ("in", None, "Plan the API"),
        ("owner", "both", "Use v2"),
    ]

    # A post to one side reaches only that side.
    aside = await post(message="Product only", to="holder")
    assert aside.json()["delivered"] == [product["id"]]

    # Noting a thread marks it read, even when the state stays the same.
    assert (await rows(product))["/Architect"]["unread"] > 0
    await _tool(auth_client, product, "sys_thread_note", counterpart="/Architect", state="open")
    await _tool(auth_client, product, "sys_thread_note", counterpart="/Architect", summary="Seen")
    assert (await rows(product))["/Architect"]["unread"] == 0

    nudge = await post(message="Architect, answer the schema question", to="counterpart")
    assert nudge.json()["delivered"] == [architect["id"]]
    assert (await rows(product))["/Architect"]["state"] == "open"
    # The thread view shows every post once, oldest first, with whom it reached.
    view = await auth_client.get(
        f"/v1/sessions/{product['id']}/threads/messages", params={"counterpart": "/Architect"}
    )
    owner_posts = [
        (m["to"], m["text"]) for m in view.json()["messages"] if m["direction"] == "owner"
    ]
    assert owner_posts == [
        ("both", "Use v2"),
        ("holder", "Product only"),
        ("counterpart", "Architect, answer the schema question"),
    ]
    assert (await post(message="x", to="nobody")).status_code == 422

    # A session without a role still files an owner post in the thread.
    plain = await _create_session_as(auth_client, "", "alice")
    _bind_runner(db_uri, plain["id"])
    to_plain = await auth_client.post(
        f"/v1/sessions/{product['id']}/threads/post",
        json={"counterpart": f"session:{plain['id']}", "message": "Hi there", "to": "counterpart"},
    )
    assert to_plain.json()["delivered"] == [plain["id"]]
    view = await auth_client.get(
        f"/v1/sessions/{product['id']}/threads/messages",
        params={"counterpart": f"session:{plain['id']}"},
    )
    assert [(m["direction"], m["to"], m["text"]) for m in view.json()["messages"]] == [
        ("owner", "counterpart", "Hi there")
    ]

    # A thread side chat writing to the counterpart speaks as its holder.
    opened = await auth_client.post(
        f"/v1/sessions/{product['id']}/fork", json={"side_chat": True, "thread": "/Architect"}
    )
    side = opened.json()
    _bind_runner(db_uri, side["id"])
    assert (await _send(auth_client, side, architect, "From the side chat")).get("queued") is True
    architect_items = (await auth_client.get(f"/v1/sessions/{architect['id']}/items")).text
    assert "[Message from /Product (session " in architect_items
    assert (await rows(product))["/Architect"]["state"] == "waiting_on_them"
    assert set(await rows(architect)) == {"/Product"}


async def test_group_thread_reaches_roles_and_sessions(auth_client, peers, db_uri):
    architect, product, _ = peers
    other = await _create_session_as(auth_client, "", "alice")
    for session in (product, other):
        _bind_runner(db_uri, session["id"])
    for session, alias in ((architect, "Architect"), (product, "Product")):
        assert (
            await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        ).status_code == 200

    async def items(session_id):
        return (await auth_client.get(f"/v1/sessions/{session_id}/items")).text

    # The owner starts a thread with one role, then invites a role and a plain session.
    created = await auth_client.post(
        "/v1/sessions/group-threads",
        json={"title": "API plan", "members": [{"kind": "role", "ref": "/Product"}]},
    )
    assert created.status_code == 201, created.text
    key = created.json()["thread"]["key"]
    base = f"/v1/sessions/group-threads/{key}"
    for kind, ref in (("role", "/Architect"), ("session", other["id"])):
        invited = await auth_client.post(
            f"{base}/members", json={"kind": kind, "ref": ref, "primer": "none"}
        )
        assert invited.status_code == 201, invited.text

    # An owner post reaches every member; roles in a side chat of their holder.
    posted = await auth_client.post(f"{base}/posts", json={"message": "Draft the schema"})
    assert posted.status_code == 201, posted.text
    reached = {row["ref"]: row["session_id"] for row in posted.json()["delivered"]}
    assert reached[other["id"]] == other["id"]
    product_side = {"id": reached["/Product"]}
    assert product_side["id"] not in (product["id"], None)
    assert "[Message from owner (message msg_" in await items(product_side["id"])
    assert f"[thread {key}]" in await items(other["id"])
    assert f"[thread {key}]" not in await items(product["id"])

    # An agent post naming nobody starts no turn; naming one member reaches only it,
    # with the posts it missed.
    _bind_runner(db_uri, product_side["id"])
    quiet = await _tool(auth_client, product_side, "sys_thread_post", thread=key, message="Noted")
    assert quiet["post"]["author_ref"] == "/Product" and quiet["delivered"] == []
    asked = await _tool(
        auth_client, other, "sys_thread_post", thread=key, message="Ready?", to=["/Architect"]
    )
    assert [row["ref"] for row in asked["delivered"]] == ["/Architect"]
    architect_items = await items(asked["delivered"][0]["session_id"])
    assert "#2 /Product: Noted" in architect_items and "Ready?" in architect_items
    history = await _tool(auth_client, other, "sys_thread_history", counterpart=key)
    assert [p["seq"] for p in history["posts"]] == [1, 2, 3]
    listed = await _tool(auth_client, other, "sys_thread_list")
    assert [row["key"] for row in listed["group_threads"]] == [key]

    # Agents alone hit the hop cap; the owner's next post resets it.
    for turn in range(4):
        assert "post" in await _tool(
            auth_client, other, "sys_thread_post", thread=key, message=f"hop {turn}"
        )
    refused = await _tool(auth_client, other, "sys_thread_post", thread=key, message="again")
    assert "ask the owner" in refused["detail"]
    reset = await auth_client.post(f"{base}/posts", json={"message": "Go on", "to": []})
    assert reset.status_code == 201
    assert "post" in await _tool(auth_client, other, "sys_thread_post", thread=key, message="ok")

    # A removed member gets nothing more and cannot post; its posts stay.
    detail = (await auth_client.get(base)).json()
    other_member = next(m for m in detail["members"] if m["ref"] == other["id"])
    assert (await auth_client.delete(f"{base}/members/{other_member['id']}")).status_code == 200
    gone = await auth_client.post(f"{base}/posts", json={"message": "Still there?"})
    assert other["id"] not in {row["ref"] for row in gone.json()["delivered"]}
    assert "detail" in await _tool(auth_client, other, "sys_thread_post", thread=key, message="x")
    posts = (await auth_client.get(f"{base}/posts")).json()["posts"]
    assert any(p["author_ref"] == other["id"] for p in posts)

    # Only the owner manages members.
    denied = await auth_client.post(
        f"{base}/members",
        json={"kind": "session", "ref": other["id"]},
        headers={ORCHESTRATION_SOURCE_HEADER: product_side["id"]},
    )
    assert denied.status_code == 403


async def test_group_thread_primer_and_role_hand_over(auth_client, peers, db_uri):
    architect, product, _ = peers
    _bind_runner(db_uri, product["id"])
    assert (
        await auth_client.put(f"/v1/sessions/{product['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    created = await auth_client.post(
        "/v1/sessions/group-threads", json={"title": "Roadmap", "message": "Kickoff"}
    )
    key = created.json()["thread"]["key"]
    base = f"/v1/sessions/group-threads/{key}"
    await auth_client.patch(base, json={"summary": "Ship v1 first"})

    # A late joiner's first event is the primer, with the summary.
    invited = await auth_client.post(f"{base}/members", json={"kind": "role", "ref": "/Product"})
    side = invited.json()["primed_session_id"]
    side_items = (await auth_client.get(f"/v1/sessions/{side}/items")).text
    assert "You were added to this thread." in side_items
    assert "Summary so far: Ship v1 first" in side_items

    # A vacant role keeps its place; the next holder gets the missed posts and a primer.
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code in (
        200,
        204,
    )
    vacant = await auth_client.post(f"{base}/posts", json={"message": "While away"})
    assert vacant.json()["delivered"][0]["session_id"] is None
    assert (
        await auth_client.put(f"/v1/sessions/{architect['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    back = await auth_client.post(f"{base}/posts", json={"message": "Welcome"})
    new_side = back.json()["delivered"][0]["session_id"]
    new_items = (await auth_client.get(f"/v1/sessions/{new_side}/items")).text
    assert "#2 owner: While away" in new_items
    assert "Summary so far: Ship v1 first" in new_items and "Welcome" in new_items


async def test_role_threads_index_history_guard_and_owner_thread(auth_client, peers, db_uri):
    architect, product, _ = peers
    other = await _create_session_as(auth_client, "", "alice")
    for session in (product, other):
        _bind_runner(db_uri, session["id"])
    for session, alias in ((architect, "Architect"), (product, "Product")):
        assert (
            await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        ).status_code == 200

    # Delivery tags the thread and indexes it on the holder; an unroled sender is session:<id>.
    assert (await _send(auth_client, architect, product, "Plan the API")).get("queued") is True
    assert (await _send(auth_client, other, product, "Hello from nowhere")).get("queued") is True
    items = (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text
    assert "message msg_" in items and "] [thread /Architect]" in items
    assert f"[thread session:{other['id']}]" in items
    threads = (await _tool(auth_client, product, "sys_thread_list"))["threads"]
    rows = {row["counterpart"]: row for row in threads}
    assert rows["/Architect"]["state"] == "waiting_on_us" and rows["/Architect"]["unread"] == 1
    assert rows["/Architect"]["summary"] == "Plan the API"
    assert rows["/Architect"]["promotion_candidate"] is False
    assert set(rows) == {"/Architect", f"session:{other['id']}"}
    sent = (await _tool(auth_client, architect, "sys_thread_list"))["threads"]
    assert [(r["counterpart"], r["state"]) for r in sent] == [("/Product", "waiting_on_them")]

    # A reply marks the thread read and waiting on them; history reads it by tag.
    assert (await _send(auth_client, product, architect, "Draft ready")).get("queued") is True
    rows = {
        r["counterpart"]: r
        for r in (await _tool(auth_client, product, "sys_thread_list"))["threads"]
    }
    assert rows["/Architect"]["state"] == "waiting_on_them" and rows["/Architect"]["unread"] == 0
    history = await _tool(auth_client, product, "sys_thread_history", counterpart="/Architect")
    assert [(m["direction"], m["text"]) for m in history["messages"]] == [
        ("in", "Plan the API"),
        ("out", "Draft ready"),
    ]
    assert history["messages"][0]["message_id"].startswith("msg_")

    # The owner's thread view shows both sides; sends are read from the receiver.
    view = await auth_client.get(
        f"/v1/sessions/{product['id']}/threads/messages", params={"counterpart": "/Architect"}
    )
    assert view.status_code == 200
    assert [(m["direction"], m["text"]) for m in view.json()["messages"]] == [
        ("in", "Plan the API"),
        ("out", "Draft ready"),
    ]
    unroled = await auth_client.get(
        f"/v1/sessions/{other['id']}/threads/messages", params={"counterpart": "/Product"}
    )
    assert unroled.status_code == 400

    # Taking a role folds the sends made before it into the role's thread, counted once.
    assert (
        await auth_client.put(f"/v1/sessions/{other['id']}/alias", json={"alias": "Builder"})
    ).status_code == 200
    rows = {
        r["counterpart"]: r
        for r in (await _tool(auth_client, product, "sys_thread_list"))["threads"]
    }
    assert set(rows) == {"/Architect", "/Builder"} and rows["/Builder"]["messages"] == 1
    early = await _tool(auth_client, product, "sys_thread_history", counterpart="/Builder")
    assert [m["text"] for m in early["messages"]] == ["Hello from nowhere"]
    assert (await auth_client.delete(f"/v1/sessions/{other['id']}/alias")).status_code == 204

    # History follows the role to a new holder session.
    successor = await _create_session_as(auth_client, "", "alice")
    _bind_runner(db_uri, successor["id"])
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(successor["id"], {"thread.role": "/Product"})
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    moved = await _tool(auth_client, successor, "sys_thread_history", counterpart="/Architect")
    assert [m["text"] for m in moved["messages"]] == ["Plan the API", "Draft ready"]
    assert moved["messages"][0]["session_id"] == product["id"]

    # A duplicate message_id is acknowledged once and delivered once.
    body = {
        "type": "session_message",
        "data": {"message": "Once", "message_id": "msg_" + "a" * 32},
    }
    event_headers = {"X-Omnigent-Orchestration-Source": architect["id"]}
    first = await auth_client.post(
        f"/v1/sessions/{successor['id']}/events", json=body, headers=event_headers
    )
    again = await auth_client.post(
        f"/v1/sessions/{successor['id']}/events", json=body, headers=event_headers
    )
    assert first.json()["queued"] is True and again.json().get("duplicate") is True
    assert (await auth_client.get(f"/v1/sessions/{successor['id']}/items")).text.count("Once") == 1

    # Role-to-role ping-pong stops at the hop cap.
    pair = (architect, successor)
    results = [
        await _send(auth_client, pair[n % 2], pair[(n + 1) % 2], f"ping {n}") for n in range(8)
    ]
    refused_at = next(n for n, r in enumerate(results) if not r.get("queued"))
    assert 0 < refused_at < 6 and all("loop guard" in json.dumps(r) for r in results[refused_at:])

    # sys_thread_note validates state and truncates the summary to a label.
    noted = await _tool(
        auth_client,
        successor,
        "sys_thread_note",
        counterpart="/Architect",
        state="done",
        summary="x" * 900,
    )
    assert noted["thread"]["state"] == "done" and len(noted["thread"]["summary"]) == 256
    assert (
        "state must be one of"
        in (
            await _tool(
                auth_client, successor, "sys_thread_note", counterpart="/Architect", state="bogus"
            )
        )["detail"]
    )

    # An owner-started thread is a labelled side chat; its note reaches the holder.
    opened = await auth_client.post(
        f"/v1/sessions/{successor['id']}/fork",
        json={"side_chat": True, "thread": "/Bench", "title": "Thread: /Bench"},
    )
    assert opened.status_code == 201, opened.text
    side = opened.json()
    _bind_runner(db_uri, side["id"])
    labels = store.get_conversation(side["id"]).labels
    assert labels["thread.of"] == "/Bench" and not [
        k for k in labels if k.startswith("thread.") and k != "thread.of"
    ]
    # The side chat names its parent and the role it does not hold.
    assert labels["omnigent.fork.of"] == successor["id"]
    assert labels["omnigent.fork.of_role"].endswith("/Product")
    assert {
        r["counterpart"]: r["state"]
        for r in (await _tool(auth_client, successor, "sys_thread_list"))["threads"]
    }["/Bench"] == "open"
    note = await _tool(
        auth_client, side, "sys_thread_note", state="blocked", summary="Needs the schema"
    )
    assert note["notified"]["queued"] is True
    assert (
        "[thread /Bench]" in (await auth_client.get(f"/v1/sessions/{successor['id']}/items")).text
    )
    first_row = (await _tool(auth_client, successor, "sys_thread_list"))["threads"][0]
    assert (first_row["counterpart"], first_row["state"]) == ("/Bench", "blocked")
    # The note line to its own holder is not an unread message.
    assert (first_row["unread"], first_row["messages"]) == (0, 0)
    # A side chat that names another thread notes that one and tells nobody.
    elsewhere = await _tool(
        auth_client, side, "sys_thread_note", counterpart="/Architect", state="waiting_on_them"
    )
    assert elsewhere["thread"]["state"] == "waiting_on_them" and "notified" not in elsewhere
    assert (await _held_row(auth_client, successor, "/Bench"))["state"] == "blocked"
    group_note = await _tool(
        auth_client, side, "sys_thread_note", counterpart="grp_" + "a" * 32, state="done"
    )
    assert "sys_thread_post" in group_note["detail"]
    # Reopening the thread returns its side chat instead of forking another.
    reopened = await auth_client.post(
        f"/v1/sessions/{successor['id']}/fork", json={"side_chat": True, "thread": "/Bench"}
    )
    assert reopened.status_code == 201 and reopened.json()["id"] == side["id"]
    # Threads need a role, and an unroled session cannot open one.
    refused = await auth_client.post(
        f"/v1/sessions/{other['id']}/fork", json={"side_chat": True, "thread": "/Bench"}
    )
    assert refused.status_code == 400


@pytest.mark.parametrize(
    "entry", ["json", "multipart", "same_host", "managed", "mcp_same_host", "mcp_managed"]
)
async def test_nested_agent_gate_tracks_request_context_before_creation(
    auth_client, auth_app, peers, db_uri, monkeypatch, entry
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import (
        _register_online_host,
        build_agent_bundle,
    )

    source, _, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    host_id = uuid4().hex
    _register_online_host(auth_app, host_id, "alice")
    store.set_host_id(source["id"], host_id, workspace="/tmp")
    launched = []

    def answer_host(connection, payload):
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        else:
            assert isinstance(frame, HostLaunchRunnerFrame)
            launched.append(frame)
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)

    async def launch(**kwargs):
        launched.append(kwargs)

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    # Embedded deployments read their rollout service from the request context.
    enabled = ContextVar("nested_agents_enabled", default=False)

    class RequestFeatureFlags(FeatureFlags):
        def enabled(self, feature):
            return (
                enabled.get() if feature == Feature.SIBLING_SESSIONS else super().enabled(feature)
            )

    auth_app.state.feature_flags = resolve_feature_flags({})
    info = (await auth_client.get("/v1/info")).json()
    assert info["sibling_sessions"] is info["features"]["sibling_sessions"] is False
    auth_app.state.feature_flags = RequestFeatureFlags(resolve_feature_flags({}).enabled_features)
    sibling_id = None
    for active in (False, True, False):
        token = enabled.set(active)
        try:
            info = (await auth_client.get("/v1/info")).json()
            assert info["sibling_sessions"] is info["features"]["sibling_sessions"] is active
            before = {row.id for row in store.list_conversations().data}
            launches_before = len(launched)
            metadata = {"sibling_source_id": source["id"]}
            if entry == "multipart":
                response = await auth_client.post(
                    "/v1/sessions",
                    data={"metadata": json.dumps(metadata)},
                    files={
                        "bundle": (
                            "agent.tar.gz",
                            build_agent_bundle(name="nested-agent"),
                            "application/gzip",
                        )
                    },
                )
            elif entry == "json":
                response = await auth_client.post(
                    "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
                )
            elif entry.startswith("mcp_"):
                result = json.loads(
                    await execute_tool(
                        tool_name="sys_session_create",
                        arguments=json.dumps(
                            {
                                "agent_id": source["agent_id"],
                                "placement": entry.removeprefix("mcp_"),
                            }
                        ),
                        server_client=auth_client,
                        conversation_id=source["id"],
                    )
                )
                assert "error" not in result, result
                created_id = result["conversation_id"]
            else:
                response = await auth_client.post(
                    "/v1/sessions/orchestrated",
                    headers={ORCHESTRATION_SOURCE_HEADER: source["id"]},
                    json={
                        "agent_id": source["agent_id"],
                        "host_type": "managed" if entry == "managed" else "external",
                    },
                )
            if not active and entry in ("json", "multipart"):
                assert response.status_code == 403, response.text
                assert "Nested agents are disabled" in response.text
                assert {row.id for row in store.list_conversations().data} == before
                assert len(launched) == launches_before
                continue
            if not entry.startswith("mcp_"):
                assert response.status_code == 201, response.text
                created_id = response.json().get("id") or response.json()["session_id"]
            created = store.get_conversation(created_id)
            assert created.parent_conversation_id is None
            assert created.family_id == (source["id"] if active else None)
            assert {row.id for row in store.list_conversations().data} == before | {created_id}
            assert all(
                grant.user_id == "alice" for grant in permissions.list_for_session(created_id)[0]
            )
            if entry not in ("json", "multipart"):
                assert len(launched) == launches_before + 1
            if active:
                sibling_id = created_id
        finally:
            enabled.reset(token)
    assert sibling_id is not None
    assert (await auth_client.get(f"/v1/sessions/{sibling_id}")).status_code == 200
    assert (await _send(auth_client, source, {"id": sibling_id})).get("queued") is True
    for parent in ({}, {"parent_session_id": source["id"]}):
        ordinary = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], **parent}
        )
        assert ordinary.status_code == 201, ordinary.text


@pytest.mark.parametrize("bundled", [False, True])
async def test_siblings_are_private_roots_and_owner_only_in_lists(
    auth_client, auth_app, peers, db_uri, bundled, monkeypatch
):
    from omnigent.server.sharing_settings import DefaultPublicSessions
    from tests.server.integration.test_sessions_permissions import build_agent_bundle

    source, target, _ = peers
    store = SqlAlchemyConversationStore(db_uri)
    invalid = await auth_client.patch(
        f"/v1/sessions/{source['id']}", json={"project_id": "", "family_pinned": True}
    )
    assert invalid.status_code == 400
    assert store.get_conversation(source["id"]).family_id is None
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("reader")
    permissions.grant("reader", source["id"], LEVEL_READ)
    auth_app.state.default_public_sessions = lambda: DefaultPublicSessions.ALL
    metadata = {"sibling_source_id": source["id"], "title": "Private sibling"}
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
        lambda: max(source["created_at"], target["created_at"]) + 1,
    )
    if bundled:
        response = await auth_client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="sibling-agent"),
                    "application/gzip",
                )
            },
        )
        sibling_id = response.json().get("session_id")
    else:
        response = await auth_client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
        )
        sibling_id = response.json().get("id")
    assert response.status_code == 201, response.text
    store = SqlAlchemyConversationStore(db_uri)
    sibling = store.get_conversation(sibling_id)
    assert sibling.parent_conversation_id is None and sibling.root_conversation_id == sibling.id
    assert sibling.family_id == source["id"]
    assert "omnigent.session_family_id" not in sibling.labels
    assert [grant.user_id for grant in permissions.list_for_session(sibling_id)[0]] == ["alice"]
    assert store.list_child_conversation_ids_by_parent([source["id"]])[source["id"]] == []
    snapshot = await auth_client.get(f"/v1/sessions/{sibling_id}")
    assert snapshot.json()["family"]["id"] == source["id"]
    listing = await auth_client.get(
        "/v1/sessions", params={"limit": 1, "search_query": "Private sibling"}
    )
    assert [row["id"] for row in listing.json()["data"]] == [sibling_id]
    expanded = await auth_client.get(
        "/v1/sessions",
        params={"limit": 1, "search_query": "Private sibling", "expand_families": True},
    )
    assert [row["id"] for row in expanded.json()["data"]] == [sibling_id]
    expanded = await auth_client.get(
        "/v1/sessions",
        params={"limit": 1, "agent_id": sibling.agent_id, "expand_families": True},
    )
    assert {row["id"] for row in expanded.json()["data"]} == {source["id"], sibling_id}
    assert expanded.json()["page_boundary"]["id"] == sibling_id
    # Filtering a cursor side chat must not move the boundary into appended family context.
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY

    with monkeypatch.context() as clock:
        clock.setattr(
            "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch",
            lambda: sibling.created_at - 1,
        )
        hidden = store.create_conversation(
            conversation_id="f" * 32,
            agent_id=sibling.agent_id,
            labels={SIDE_CHAT_LABEL_KEY: "true"},
        )
    permissions.grant("alice", hidden.id, LEVEL_OWNER)
    filtered = (
        await auth_client.get(
            "/v1/sessions",
            params={"limit": 2, "agent_id": sibling.agent_id, "expand_families": True},
        )
    ).json()
    assert filtered["last_id"] == hidden.id
    assert filtered["page_boundary"] == {
        "id": sibling_id,
        "created_at": sibling.created_at,
        "updated_at": sibling.updated_at,
    }
    assert {row["id"] for row in filtered["data"]} == {source["id"], sibling_id}
    pinned = await auth_client.patch(
        f"/v1/sessions/{sibling_id}", json={"labels": {"omnigent.pinned": "1"}}
    )
    assert pinned.status_code == 200, pinned.text
    for expand in (False, True):
        pinned_page = await auth_client.get(
            "/v1/sessions", params={"pinned": True, "expand_families": expand}
        )
        assert {row["id"] for row in pinned_page.json()["data"]} == (
            {source["id"], sibling_id} if expand else {sibling_id}
        )
    ids = []
    after = None
    while True:
        page = (
            await auth_client.get(
                "/v1/sessions", params={"limit": 1, **({"after": after} if after else {})}
            )
        ).json()
        assert len(page["data"]) <= 1
        ids.extend(row["id"] for row in page["data"])
        if not page["has_more"]:
            break
        after = page["last_id"]
    assert len(ids) == len(set(ids))
    assert {source["id"], sibling_id} <= set(ids)
    for pin_request in ({"family_pinned": True}, {"labels": {"omnigent.pinned": "1"}}):
        refused_pin = await auth_client.patch(
            f"/v1/sessions/{source['id']}", json={**pin_request, "reasoning_effort": "bogus"}
        )
        assert refused_pin.status_code == 400
        assert not (await auth_client.get(f"/v1/sessions/{source['id']}")).json()["family"][
            "pinned"
        ]
    reader = {"X-Forwarded-Email": "reader"}
    snapshot = await auth_client.get(f"/v1/sessions/{source['id']}", headers=reader)
    assert snapshot.status_code == 200
    assert not snapshot.json().get("family")
    assert "family_id" not in snapshot.json()
    assert "omnigent.session_family_id" not in snapshot.json()["labels"]
    response = await auth_client.patch(
        f"/v1/sessions/{source['id']}", json={"family_id": sibling_id}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "family_id"]
    assert store.get_conversation(source["id"]).family_id == source["id"]

    host_id = uuid4().hex
    auth_app.state.host_store.register_managed_host(
        host_id=host_id,
        name="Other sandbox",
        user_id="alice",
        token="test-token",
        provider="modal",
        sandbox_id="other-sandbox",
        token_expires_at=2_000_000_000,
    )
    refused = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            **metadata,
            "host_id": host_id,
            "workspace": "/work",
        },
    )
    assert refused.status_code == 403, refused.text
    assert "Only the source session's sandbox" in refused.text
    side = await auth_client.post(f"/v1/sessions/{source['id']}/fork", json={"side_chat": True})
    assert side.status_code == 201, side.text
    from_side = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "sibling_source_id": side.json()["id"]},
    )
    assert from_side.status_code == 201, from_side.text
    assert store.get_conversation(from_side.json()["id"]).family_id == source["id"]

    monkeypatch.setattr("omnigent.server.routes.sessions.routes_core._SESSION_LIST_BATCH_SIZE", 2)
    monkeypatch.setattr("omnigent.server.session_families._FAMILY_GRANT_BATCH_SIZE", 2)
    list_grants = SqlAlchemyPermissionStore.list_for_sessions
    grant_sizes = []
    is_admin = SqlAlchemyPermissionStore.is_admin
    admin_checks = []

    def counted_is_admin(self, user_id):
        admin_checks.append(user_id)
        return is_admin(self, user_id)

    def bounded_grants(self, ids, *, user_id=None):
        grant_sizes.append(len(ids))
        assert len(ids) <= 2
        return list_grants(self, ids, user_id=user_id)

    monkeypatch.setattr(SqlAlchemyPermissionStore, "list_for_sessions", bounded_grants)
    monkeypatch.setattr(SqlAlchemyPermissionStore, "is_admin", counted_is_admin)
    expanded = await auth_client.get("/v1/sessions", params={"expand_families": True})
    assert expanded.status_code == 200, expanded.text
    assert {row["id"] for row in expanded.json()["data"]} == {
        source["id"],
        peers[1]["id"],
        sibling_id,
        from_side.json()["id"],
    }
    assert grant_sizes.count(2) >= 3
    assert admin_checks == ["alice"]
    permissions.grant("reader", source["id"], LEVEL_EDIT)
    response = await auth_client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "policy,target,allowed,transport",
    [
        (policy, target, allowed, transport)
        for policy, target, allowed in [
            ("current_host", "current", True),
            ("current_host", "other", False),
            ("current_host", "managed", False),
            ("current_host", "unbound", False),
            ("current_host_or_new_sandbox", "current", True),
            ("current_host_or_new_sandbox", "other", False),
            ("current_host_or_new_sandbox", "managed", True),
            ("current_host_or_new_sandbox", "unbound", False),
            ("any_host", "current", True),
            ("any_host", "other", True),
            ("any_host", "managed", True),
            ("any_host", "unbound", True),
        ]
        for transport in ("json", "bundled", "orchestrated")
        if transport != "orchestrated" or target in {"current", "managed"}
    ],
)
async def test_sibling_destination_policy_is_enforced_before_creation(
    auth_client, auth_app, peers, db_uri, monkeypatch, policy, target, allowed, transport
):
    from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
    from omnigent.server.routes.sessions import routes_core
    from omnigent.server.sharing_settings import SiblingSessionDestinations
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, _ = peers
    source_host, other_host = uuid4().hex, uuid4().hex
    for host_id in (source_host, other_host):
        _register_online_host(auth_app, host_id, "alice")
        auth_app.state.host_store.upsert_on_connect(host_id, host_id, "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(source["id"], source_host, workspace="/source")
    auth_app.state.sibling_session_destinations = lambda: SiblingSessionDestinations(policy)

    traffic = []

    def answer_host(connection, payload):
        traffic.append(payload)
        frame = decode_host_frame(payload)
        if isinstance(frame, HostStatFrame):
            connection.pending_stats[frame.request_id].set_result(
                {"status": "ok", "exists": True, "type": "directory", "canonical_path": frame.path}
            )
        elif isinstance(frame, HostLaunchRunnerFrame):
            connection.pending_launches[frame.request_id].set_result({"status": "launched"})
        else:
            pytest.fail(f"Unexpected host frame: {frame}")

    monkeypatch.setattr(auth_app.state.host_registry, "send_text", answer_host)

    async def finish_managed_launch(**kwargs):
        traffic.append(kwargs)

    monkeypatch.setattr(routes_core, "_run_managed_launch", finish_managed_launch)
    body = {"agent_id": source["agent_id"], "sibling_source_id": source["id"]}
    if target in {"current", "other"}:
        body.update(
            {
                "host_id": source_host if target == "current" else other_host,
                "workspace": f"/{target}",
            }
        )
    elif target == "managed":
        body["host_type"] = "managed"

    before = {session.id for session in store.list_conversations().data}
    if transport == "bundled":
        from tests.server.integration.test_sessions_permissions import build_agent_bundle

        body.pop("agent_id")
        response = await auth_client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(body)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="policy-worker"),
                    "application/gzip",
                )
            },
        )
    elif transport == "orchestrated":
        from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER

        # Runners identify the source through the header; the server derives placement.
        for key in ("sibling_source_id", "host_id", "workspace"):
            body.pop(key, None)
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            json=body,
            headers={
                ORCHESTRATION_SOURCE_HEADER: source["id"],
            },
        )
    else:
        response = await auth_client.post("/v1/sessions", json=body)
    assert response.status_code == (201 if allowed else 403), response.text
    created = {session.id for session in store.list_conversations().data} - before
    assert len(created) == int(allowed)
    if not allowed:
        assert "server policy" in response.text
        assert traffic == []


@pytest.mark.parametrize(
    "policy,binding,enabled,allowed",
    [
        (policy, binding, True, binding == "same")
        for policy in ("current_host", "current_host_or_new_sandbox")
        for binding in ("same", "other", "unbound", "unknown", "ambiguous")
    ]
    + [
        ("any_host", "unbound", True, True),
        ("current_host", "unbound", False, True),
    ],
)
async def test_sibling_destination_policy_checks_runner_reassignment(
    auth_client, auth_app, peers, db_uri, policy, binding, enabled, allowed
):
    from omnigent.server.sharing_settings import SiblingSessionDestinations

    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    host_id = uuid4().hex
    if binding != "unknown":
        store.set_host_id(source["id"], host_id, workspace="/source")
    if binding != "unbound":
        target_host = uuid4().hex if binding == "other" else host_id
        store.set_host_id(target["id"], target_host, workspace="/target")
    runner_id = store.get_conversation(source["id"]).runner_id
    if binding == "ambiguous":
        store.create_conversation(
            agent_id=None, host_id=uuid4().hex, workspace="/other", runner_id=runner_id
        )
    store.add_new_sibling(source["id"], target["id"], user_id="alice")
    auth_app.state.sibling_session_destinations = lambda: SiblingSessionDestinations(policy)
    if not enabled:
        auth_app.state.feature_flags = resolve_feature_flags(
            {FEATURES_ENV_VAR: "-sibling_sessions"}
        )
    previous = store.get_conversation(target["id"])
    captured.clear()

    response = await auth_client.patch(
        f"/v1/sessions/{target['id']}", json={"runner_id": runner_id}
    )

    assert response.status_code == (200 if allowed else 403), response.text
    saved = store.get_conversation(target["id"])
    assert saved.host_id == previous.host_id
    assert saved.workspace == previous.workspace
    if allowed:
        assert saved.runner_id == runner_id
        assert captured.get("posts")
    else:
        assert saved.runner_id == previous.runner_id
        assert "server policy" in response.text
        assert not captured.get("posts")


async def test_family_project_removal_precedes_archive(auth_client, peers, db_uri):
    source, _, _ = peers
    response = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": source["agent_id"],
            "sibling_source_id": source["id"],
        },
    )
    assert response.status_code == 201, response.text
    sibling_id = response.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    pinned = await auth_client.patch(
        f"/v1/sessions/{source['id']}",
        json={"labels": {"omnigent.pinned": "1" * 300}, "title": "Pinned family"},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["title"] == "Pinned family"
    for session_id in (source["id"], sibling_id):
        snapshot = (await auth_client.get(f"/v1/sessions/{session_id}")).json()
        assert snapshot["family"]["pinned_at"] == "1" * 256
    # Filing only targets a live project.
    SqlAlchemyProjectStore(db_uri).create("a" * 32, "Work", None)
    assert store.set_conversation_project(source["id"], "a" * 32)
    refused = await auth_client.patch(
        f"/v1/sessions/{source['id']}",
        json={"project_id": "", "labels": {"omni_project": "Stray"}, "title": "Rejected"},
    )
    assert refused.status_code == 400, refused.text
    for member in store.get_session_family(source["id"]):
        assert member.project_id == "a" * 32
        assert member.labels.get("omni_project") != "Stray"
    assert store.get_conversation(source["id"]).title == "Pinned family"
    refused = await auth_client.patch(f"/v1/sessions/{sibling_id}", json={"project_id": ""})
    assert refused.status_code == 409, refused.text
    removed = await auth_client.patch(
        f"/v1/sessions/{source['id']}",
        json={
            "archived": True,
            "project_id": "",
            "labels": {"omni_project": ""},
        },
    )
    assert removed.status_code == 200, removed.text
    assert store.get_conversation(source["id"]).archived
    assert not store.get_conversation(sibling_id).archived
    assert store.get_conversation(sibling_id).project_id is None
    store.update_conversation(sibling_id, archived=True)
    snapshot = (await auth_client.get(f"/v1/sessions/{source['id']}")).json()
    assert snapshot["family"]["representative_id"] == source["id"]

    unpinned = await auth_client.patch(
        f"/v1/sessions/{source['id']}", json={"labels": {"omnigent.pinned": ""}}
    )
    assert unpinned.status_code == 200, unpinned.text
    for session_id in (source["id"], sibling_id):
        snapshot = (await auth_client.get(f"/v1/sessions/{session_id}")).json()
        assert not snapshot["family"]["pinned"]
        store.update_conversation(session_id, archived=False)
    snapshot = (await auth_client.get(f"/v1/sessions/{source['id']}")).json()
    assert not snapshot["family"]["pinned"]


async def _role_pair(auth_client, peers, db_uri):
    architect, product, _ = peers
    _bind_runner(db_uri, product["id"])
    for session, alias in ((architect, "Architect"), (product, "Product")):
        assert (
            await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        ).status_code == 200
    return architect, product


async def _held_row(auth_client, session, counterpart):
    threads = (await _tool(auth_client, session, "sys_thread_list"))["threads"]
    return next(row for row in threads if row["counterpart"] == counterpart)


async def test_loop_guard_counts_short_messages_and_holds_the_seventh(auth_client, peers, db_uri):
    architect, product = await _role_pair(auth_client, peers, db_uri)
    pair = (architect, product)
    long_text = "Security finding: " + "detail " * 60
    for n in range(7):  # substantive messages never count toward the loop cap
        sent = await _send(auth_client, pair[n % 2], pair[(n + 1) % 2], f"{long_text} {n}")
        assert sent.get("queued") is True
    for n in range(6):
        sent = await _send(auth_client, pair[n % 2], pair[(n + 1) % 2], f"ok {n}")
        assert sent.get("queued") is True
    held = await _send(auth_client, architect, product, "ok 6")
    assert "held for the owner" in json.dumps(held) and "Don't resend" in json.dumps(held)
    assert "/Architect \\u2194 /Product" in json.dumps(held)
    row = await _held_row(auth_client, architect, "/Product")
    assert row["state"] == "waiting_on_owner"
    assert row["held"]["held_to"] == product["id"] and row["held"]["text"] == "ok 6"
    replaced = await _send(auth_client, architect, product, "ok 7")
    assert "replaces the message held earlier" in json.dumps(replaced)
    assert (await _held_row(auth_client, architect, "/Product"))["held"]["text"] == "ok 7"
    items = (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text
    assert "ok 6" not in items

    # A peer message re-sent after a hub restart is not owner input: the guard still holds.
    header = "[Message from /Architect (session s1, message msg_1)] [thread /Architect]"
    [resent_block] = add_delivery_markers(
        [{"type": "input_text", "text": f"{header}\nok 5"}], [RESENT_NOTICE]
    )
    resent = {"type": "message", "data": {"role": "user", "content": [resent_block]}}
    posted = await auth_client.post(f"/v1/sessions/{product['id']}/events", json=resent)
    assert posted.status_code in (200, 202), posted.text
    still_held = await _send(auth_client, architect, product, "ok 8")
    assert "replaces the message held earlier" in json.dumps(still_held)

    # An owner message into either session restarts the counters.
    owner_message = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "carry on"}]},
    }
    posted = await auth_client.post(f"/v1/sessions/{product['id']}/events", json=owner_message)
    assert posted.status_code in (200, 202), posted.text
    assert (await _send(auth_client, architect, product, "ok again")).get("queued") is True


async def test_loop_guard_burst_backstop_holds_the_twenty_first_message(
    auth_client, peers, db_uri
):
    architect, product = await _role_pair(auth_client, peers, db_uri)
    pair = (architect, product)
    for n in range(20):
        sent = await _send(
            auth_client, pair[n % 2], pair[(n + 1) % 2], f"Finding {n}: " + "y" * 300
        )
        assert sent.get("queued") is True
    held = await _send(auth_client, architect, product, "Finding 20: " + "y" * 300)
    assert "held for the owner" in json.dumps(held) and "20 messages" in json.dumps(held)


async def test_owner_releases_allows_or_discards_a_held_message(auth_client, peers, db_uri):
    architect, product = await _role_pair(auth_client, peers, db_uri)

    async def trip(text):
        for n in range(6):
            sender, receiver = (architect, product) if n % 2 == 0 else (product, architect)
            if (await _row_hops_ok(sender, receiver, f"fill {n}")) is False:
                break
        return await _send(auth_client, architect, product, text)

    async def _row_hops_ok(sender, receiver, text):
        return (await _send(auth_client, sender, receiver, text)).get("queued") is True

    async def act(action):
        return await auth_client.post(
            f"/v1/sessions/{architect['id']}/threads/held",
            json={"counterpart": "/Product", "action": action},
        )

    assert (await act("release")).status_code == 409  # nothing held yet

    await trip("first held")
    discarded = await act("discard")
    assert discarded.status_code == 200 and discarded.json()["delivered"] == []
    row = await _held_row(auth_client, architect, "/Product")
    assert row["held"] is None and row["state"] == "open"
    assert (
        "owner discarded your held message"
        in (await auth_client.get(f"/v1/sessions/{architect['id']}/items")).text
    )
    assert "first held" not in (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text

    await _send(auth_client, architect, product, "second held")
    released = await act("release")
    assert released.status_code == 200 and released.json()["delivered"] == [product["id"]]
    assert "second held" in (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text
    row = await _held_row(auth_client, architect, "/Product")
    assert row["held"] is None and row["state"] == "waiting_on_them"
    # Release leaves the counters alone, so the next short message is held again.
    assert "held for the owner" in json.dumps(
        await _send(auth_client, architect, product, "third held")
    )

    allowed = await act("allow")
    assert allowed.status_code == 200 and allowed.json()["delivered"] == [product["id"]]
    assert "third held" in (await auth_client.get(f"/v1/sessions/{product['id']}/items")).text
    assert (await _send(auth_client, architect, product, "after allow")).get("queued") is True


async def _send_to_address(client, source, address, message):
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": address, "message": message}),
            server_client=client,
            conversation_id=source["id"],
        )
    )


async def _role_mail_setup(auth_client, auth_app, peers, db_uri, monkeypatch):
    """Architect sends to a busy Product; a successor session waits on the same host."""
    from unittest.mock import AsyncMock

    from omnigent.host.frames import HostHelloFrame

    architect, product, captured = peers
    successor = await _create_session_as(auth_client, "", "alice")
    store = SqlAlchemyConversationStore(db_uri)
    host_id = uuid4().hex
    for session in (product, successor):
        store.set_host_id(session["id"], host_id, "/tmp/inbox-workspace")
        store.set_session_live_status(session["id"], "running")
    auth_app.state.host_registry.register(
        host_id,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="inbox", runners=[]),
        owner=None,
    )
    monkeypatch.setattr(auth_app.state.startup_inputs, "host_changed", lambda *_: None)
    for session, alias in ((architect, "Architect"), (product, "Product")):
        assert (
            await auth_client.put(f"/v1/sessions/{session['id']}/alias", json={"alias": alias})
        ).status_code == 200
    return architect, product, successor, store, captured


def _texts(items):
    return [item.data.content[0]["text"] for item in items]


async def test_role_mail_follows_the_role_to_its_next_holder(
    auth_client, auth_app, peers, db_uri, monkeypatch
):
    from omnigent.server.role_threads import thread_of_header

    architect, product, successor, store, captured = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    startup = auth_app.state.startup_inputs
    for text in ("first", "second"):
        assert (await _send_to_address(auth_client, architect, "Product", text))["queued"]
    assert (await _send(auth_client, architect, product, "by id"))["queued"]
    rows = store.list_pending_inputs(product["id"])
    role_id = store.get_conversation(product["id"]).labels["omnigent.role_id"]
    assert [row.data.startup_options.role_id for row in rows] == [role_id, role_id, None]
    original = _texts(rows)

    # Released: the role's rows park and stop blocking the session's own message.
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    queued, _ = await startup.snapshot(product["id"])
    assert len(queued) == 1 and "by id" in json.dumps(queued)
    await startup._deliver(auth_app, product["id"], rows[0])
    assert [r.id for r in store.list_pending_inputs(product["id"])] == [r.id for r in rows]

    # The next holder gets both, once, in order, header first and marked.
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    assert _texts(store.list_pending_inputs(product["id"])) == [original[2]]
    moved = store.list_pending_inputs(successor["id"])
    assert [row.id for row in moved] == [rows[0].id, rows[1].id]
    for before, after in zip(original[:2], _texts(moved), strict=True):
        header, body = before.split("\n", 1)
        assert after == f"{header}\n[Sent to /Product before you held it]\n{body}"
        assert thread_of_header(after) == "/Architect"
    assert all(row.data.startup_options.moved for row in moved)

    # Another sweep moves nothing more; delivery happens on the new holder.
    await startup.sweep_role_mail(auth_app)
    assert len(store.list_pending_inputs(successor["id"])) == 2
    await startup._deliver(auth_app, successor["id"], moved[0])
    delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
    assert len(delivered) == 1 and "before you held it" in json.dumps(delivered)


async def test_role_mail_sweep_repairs_a_missed_move(
    auth_client, auth_app, peers, db_uri, monkeypatch
):
    architect, product, successor, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    assert (await _send_to_address(auth_client, architect, "Product", "queued"))["queued"]
    monkeypatch.setattr(session_aliases, "_bring_role_mail", lambda *_: None)
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    assert len(store.list_pending_inputs(product["id"])) == 1

    await auth_app.state.startup_inputs.sweep_role_mail(auth_app)
    assert store.list_pending_inputs(product["id"]) == []
    assert len(store.list_pending_inputs(successor["id"])) == 1


@pytest.mark.parametrize("holder_closed", [False, True])
async def test_claimed_role_mail_moves_only_from_a_closed_holder(
    auth_client, auth_app, peers, db_uri, monkeypatch, holder_closed
):
    from omnigent.server.startup_inputs import MOVED_FROM_CLOSED_NOTICE

    architect, product, successor, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    assert (await _send_to_address(auth_client, architect, "Product", "claimed"))["queued"]
    [row] = store.list_pending_inputs(product["id"])
    assert store.transition_pending_input(
        product["id"], row.id, expected="in_progress", status="incomplete"
    )
    if holder_closed:
        store.set_labels(product["id"], {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    else:
        assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200

    if not holder_closed:
        # A live holder keeps its claimed row: it still blocks the queue and is shown.
        assert [r.id for r in store.list_pending_inputs(product["id"])] == [row.id]
        assert store.list_pending_inputs(successor["id"]) == []
        startup = auth_app.state.startup_inputs
        assert await startup.has_undelivered(product["id"])
        queued, _ = await startup.snapshot(product["id"])
        assert "claimed" in json.dumps(queued)
        return
    assert store.list_pending_inputs(product["id"]) == []
    [moved] = store.list_pending_inputs(successor["id"])
    assert moved.status == "in_progress"
    lines = _texts([moved])[0].split("\n")
    assert lines[1:3] == ["[Sent to /Product before you held it]", MOVED_FROM_CLOSED_NOTICE]


async def test_role_mail_claim_fails_once_the_session_lost_the_role(
    auth_client, auth_app, peers, db_uri, monkeypatch
):
    from starlette.datastructures import Headers

    from omnigent.server.startup_inputs import StartupInputContext

    architect, product, _, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    assert (await _send_to_address(auth_client, architect, "Product", "racing"))["queued"]
    [row] = store.list_pending_inputs(product["id"])
    context = StartupInputContext(
        auth_app, Headers(), "alice", row.id, role_id=row.data.startup_options.role_id
    )
    # Released between dispatch and claim: the claim refuses and the row stays queued.
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    assert not await context.claim(store, product["id"])
    assert [r.status for r in store.list_pending_inputs(product["id"])] == ["in_progress"]
    assert (
        await auth_client.put(f"/v1/sessions/{product['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    assert await context.claim(store, product["id"])


@pytest.mark.parametrize("form", ["hyphenated", "uppercase"])
async def test_mail_sent_to_a_session_id_stays_when_the_role_moves(
    auth_client, auth_app, peers, db_uri, monkeypatch, form
):
    from uuid import UUID

    architect, product, successor, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    address = str(UUID(product["id"])) if form == "hyphenated" else product["id"].upper()
    assert (await _send_to_address(auth_client, architect, address, "by id"))["queued"]
    [row] = store.list_pending_inputs(product["id"])
    assert row.data.startup_options.role_id is None

    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    assert [r.id for r in store.list_pending_inputs(product["id"])] == [row.id]
    assert store.list_pending_inputs(successor["id"]) == []


async def test_role_mail_without_a_holder_shows_as_waiting_until_one_takes_the_role(
    auth_client, auth_app, peers, db_uri, monkeypatch
):
    architect, product, successor, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    assert (await _send_to_address(auth_client, architect, "Product", "anyone there"))["queued"]
    [row] = store.list_pending_inputs(product["id"])
    assert row.data.startup_options.sender_id == architect["id"]
    thread = row.data.startup_options.sender_thread
    assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204

    def waiting_on_thread(threads):
        return next(t["waiting_holder"] for t in threads if t["counterpart"] == thread)

    assert (
        waiting_on_thread((await _tool(auth_client, architect, "sys_thread_list"))["threads"]) == 1
    )
    history = await _tool(auth_client, architect, "sys_thread_history", counterpart=thread)
    [waiting] = [m for m in history["messages"] if m.get("waiting_holder")]
    assert (waiting["direction"], waiting["text"]) == ("out", "anyone there")
    snapshot = (await auth_client.get(f"/v1/sessions/{product['id']}")).json()
    assert snapshot["waiting_holder_inputs"] == 1

    async def holder_waits():
        listed_response = await auth_client.get("/v1/sessions/waiting")
        one_response = await auth_client.get(f"/v1/sessions/{architect['id']}/waiting")
        assert (listed_response.status_code, one_response.status_code) == (200, 200)
        listed, one = listed_response.json()["sessions"], one_response.json()
        from_list = [
            item
            for session in listed
            if session["session_id"] == architect["id"]
            for item in session["items"]
            if item["kind"] == "waiting_holder"
        ]
        assert from_list == [i for i in one["items"] if i["kind"] == "waiting_holder"]
        return [(i["ref"], i["needs_owner"]) for i in from_list]

    assert await holder_waits() == [(thread, True)]

    # A failing role-mail lookup drops only the holder items, not the routes.
    with monkeypatch.context() as patch:

        def broken(*_args, **_kwargs):
            raise RuntimeError("role store down")

        patch.setattr("omnigent.server.role_mail.waiting_by_sender", broken)
        assert await holder_waits() == []
    assert await holder_waits() == [(thread, True)]

    # A new holder clears it with no notice: the message moved and is queued normally.
    assert (
        await auth_client.put(f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"})
    ).status_code == 200
    assert (
        waiting_on_thread((await _tool(auth_client, architect, "sys_thread_list"))["threads"]) == 0
    )
    assert (await auth_client.get(f"/v1/sessions/{product['id']}")).json()[
        "waiting_holder_inputs"
    ] == 0
    assert await holder_waits() == []


@pytest.mark.parametrize("case", ["vacant", "successor", "parent_deleted"])
async def test_deleting_a_session_moves_or_drops_its_role_mail(
    auth_client, auth_app, peers, db_uri, monkeypatch, caplog, case
):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlConversation

    architect, product, successor, store, _ = await _role_mail_setup(
        auth_client, auth_app, peers, db_uri, monkeypatch
    )
    sent = await _send_to_address(auth_client, architect, "Product", "before the delete")
    [row] = store.list_pending_inputs(product["id"])
    thread = row.data.startup_options.sender_thread
    doomed = product["id"]
    if case in ("successor", "parent_deleted"):
        monkeypatch.setattr(session_aliases, "_bring_role_mail", lambda *_: None)
        assert (await auth_client.delete(f"/v1/sessions/{product['id']}/alias")).status_code == 204
        assert (
            await auth_client.put(
                f"/v1/sessions/{successor['id']}/alias", json={"alias": "Product"}
            )
        ).status_code == 200
    if case == "parent_deleted":
        # The holder is in the deleted subtree too, so the role counts as vacant.
        doomed = (await _create_session_as(auth_client, "", "alice"))["id"]
        with store._engine.begin() as connection:
            connection.execute(
                update(SqlConversation)
                .where(SqlConversation.id.in_([product["id"], successor["id"]]))
                .values(parent_conversation_id=doomed, root_conversation_id=doomed)
            )

    assert (await auth_client.delete(f"/v1/sessions/{doomed}")).status_code == 200
    [listed] = [
        t
        for t in (await _tool(auth_client, architect, "sys_thread_list"))["threads"]
        if t["counterpart"] == thread
    ]
    if case == "successor":
        assert [r.id for r in store.list_pending_inputs(successor["id"])] == [row.id]
        assert listed["dropped"] == 0
        return
    assert listed["dropped"] == 1
    assert sent["message_id"] in caplog.text

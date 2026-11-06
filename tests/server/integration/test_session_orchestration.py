"""Runner dispatch through authenticated routes and real ownership/sharing stores."""

import json
from uuid import uuid4

import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.server import session_aliases
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.feature_flags import FeatureFlags
from omnigent.stores.conversation_store import (
    ALIAS_LABEL_KEY,
    CREATED_BY_SESSION_LABEL_KEY,
    EXECUTOR_LABEL_KEY,
    PROJECT_LABEL_KEY,
    ROLE_ID_LABEL_KEY,
    ROLE_OVERLAY_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.role_store.sqlalchemy_store import SqlAlchemyRoleStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
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
def orchestration_environment(monkeypatch, request):
    features = getattr(request, "param", None)
    if features is None:
        monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_FEATURES", features)
    return features


@pytest.fixture
async def peers(auth_client, auth_app, db_uri, monkeypatch):
    source = await _create_session_as(auth_client, "", "alice")
    target = await _create_session_as(auth_client, "", "alice")
    headers = {"X-Forwarded-Email": "alice", **_bind_runner(db_uri, source["id"])}
    auth_client.headers.update(headers)
    captured = _stub_late_runner_bind(monkeypatch)
    return source, target, captured


async def _send(client, source, target, message="Please inspect the PR"):
    return json.loads(
        await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": target["id"], "message": message}),
            server_client=client,
            conversation_id=source["id"],
        )
    )


@pytest.mark.parametrize("policy_denied", [False, True])
@pytest.mark.parametrize(
    "orchestration_environment",
    [None, "", "usage_page", "-session_orchestration"],
    indirect=True,
)
async def test_private_sender_can_message_shared_receiver(
    auth_client, peers, db_uri, monkeypatch, policy_denied, orchestration_environment
):
    from omnigent.server.routes.sessions import routes_events

    source, target, captured = peers
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
    permissions.ensure_user("zed")
    permissions.grant("zed", source["id"], LEVEL_EDIT)
    _, cursor = permissions.list_for_session(source["id"])
    assert cursor is not None
    captured.clear()
    assert "shared with edit access cannot" in (await _send(auth_client, source, target))["detail"]
    assert not captured.get("posts")


async def test_sharing_during_receiver_startup_blocks_delivery(
    auth_client, peers, db_uri, monkeypatch
):
    from omnigent.server.routes import sessions

    source, target, captured = peers
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")

    async def share_while_starting(*args, **kwargs):
        permissions.grant("bob", source["id"], LEVEL_EDIT)

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


@pytest.mark.parametrize("source_link", ["missing", "cycle"])
async def test_side_chat_requires_resolvable_source(auth_client, peers, db_uri, source_link):
    source, target, captured = peers
    response = await auth_client.post(
        f"/v1/sessions/{source['id']}/fork", json={"side_chat": True}
    )
    assert response.status_code == 201, response.text
    side = response.json()
    _bind_runner(db_uri, side["id"])
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(
        side["id"],
        {SIDE_CHAT_SOURCE_LABEL_KEY: side["id"] if source_link == "cycle" else uuid4().hex},
    )
    captured.clear()
    assert (
        "Session is not owned or available" in (await _send(auth_client, side, target))["detail"]
    )
    assert not captured.get("posts")


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
@pytest.mark.parametrize(
    "condition,orchestration_environment",
    [("flag_off", "-session_orchestration"), ("shared", None), ("binding", None)],
    indirect=["orchestration_environment"],
)
async def test_creation_refuses_before_launch(
    auth_client, auth_app, peers, db_uri, placement, condition
):
    from tests.server.integration.test_sessions_permissions import _register_online_host

    source, _, captured = peers
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
        response = await auth_client.post(
            "/v1/sessions/orchestrated",
            headers={ORCHESTRATION_SOURCE_HEADER: manager["id"]},
            json={"agent_id": source["agent_id"]},
        )
        assert response.status_code == 201, response.text
        created_id = response.json()["id"]
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


@pytest.mark.parametrize("share_before_admission", [False, True])
async def test_startup_queue_admission_and_replay(
    auth_client, auth_app, peers, db_uri, monkeypatch, share_before_admission
):
    from omnigent.server.startup_inputs import STARTUP_LABEL

    source, target, captured = peers
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(target["id"], {STARTUP_LABEL: "pending"})
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("bob")
    startup = auth_app.state.startup_inputs
    accepts = startup.accepts

    async def check_startup(*args):
        result = await accepts(*args)
        assert result is True
        if share_before_admission:
            permissions.grant("bob", source["id"], LEVEL_EDIT)
        return result

    monkeypatch.setattr(startup, "accepts", check_startup)
    result = await _send(auth_client, source, target)
    pending = store.list_pending_inputs(target["id"])
    assert not captured.get("posts")
    if share_before_admission:
        assert "shared with edit access cannot" in result["detail"]
        assert pending == []
    else:
        assert result["queued"] is True
        assert len(pending) == 1
        permissions.grant("bob", source["id"], LEVEL_EDIT)
        await startup._deliver(auth_app, target["id"], pending[0])
        await startup._deliver(auth_app, target["id"], pending[0])
        delivered = [body for path, body in captured.get("posts", []) if path.endswith("/events")]
        assert len(delivered) == 1
        assert f"[Message from session {source['id']}, message msg_" in json.dumps(delivered)
        assert store.list_pending_inputs(target["id"]) == []
        assert (
            "shared with edit access cannot"
            in (await _send(auth_client, source, target))["detail"]
        )


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

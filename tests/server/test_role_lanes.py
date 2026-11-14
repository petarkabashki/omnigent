"""Role lanes (C31): claim, refusal, close and the holder-change invariants."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.builtins.messaging import MessageRoute
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes.sessions import routes_events
from omnigent.server.session_aliases import (
    AddressTarget,
    Namespace,
    claim_lane,
    list_namespace_roles,
    messaging_route_resolver,
    release_session_role,
    resolve_address_target,
    set_session_alias,
)
from omnigent.stores.conversation_store import LANE_NAME_LABEL_KEY, ROLE_LANE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.tools.builtins.role_list import compact_role_listing
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

OWNER = "alice"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"
ROOT = Namespace("", "")


@pytest.fixture()
def stores(db_uri: str) -> SimpleNamespace:
    projects = SqlAlchemyProjectStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(OWNER)
    return SimpleNamespace(
        conversations=SqlAlchemyConversationStore(db_uri),
        permissions=permissions,
        projects=projects,
        roles=projects.roles,
    )


def _session(stores: SimpleNamespace, title: str = "s") -> str:
    conversation = stores.conversations.create_conversation(title=title, agent_id=AGENT_ID)
    stores.permissions.grant(OWNER, conversation.id, LEVEL_OWNER)
    return conversation.id


def _get(stores: SimpleNamespace, session_id: str):
    return stores.conversations.get_conversation(session_id)


def _holder(stores: SimpleNamespace, role: str = "Architect") -> str:
    session = _session(stores, role)
    set_session_alias(
        stores.conversations,
        stores.projects,
        session=_get(stores, session),
        user_id=OWNER,
        alias=role,
    )
    return session


def _claim(
    stores: SimpleNamespace,
    lane_session: str,
    lane: object = "c30",
    *,
    by: str | None = None,
    **kw,
):
    return claim_lane(
        stores.conversations,
        stores.projects,
        lane_session=_get(stores, lane_session),
        namespace=ROOT,
        user_id=OWNER,
        role_name=kw.pop("role", "Architect"),
        lane=lane,
        by_holder=_get(stores, by) if by else None,
        **kw,
    )


def _close(stores: SimpleNamespace, session_id: str) -> None:
    stores.conversations.set_labels(session_id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    routes_events._release_role_on_close(stores.conversations, stores.projects, session_id)


def test_holder_claims_a_lane_and_the_labels_mirror_the_row(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker = _session(stores)

    role, lane = _claim(stores, worker, by=holder, purpose="host flap", item="C30")

    assert (lane.name, lane.session_id, lane.purpose, lane.item) == (
        "c30",
        worker,
        "host flap",
        "C30",
    )
    assert stores.roles.find_lane(role.id, "c30") == lane
    labels = _get(stores, worker).labels
    assert (labels[ROLE_LANE_LABEL_KEY], labels[LANE_NAME_LABEL_KEY]) == ("/Architect", "c30")
    # Re-claiming the same lane is idempotent.
    assert _claim(stores, worker, by=holder)[1].id == lane.id


def test_owner_claims_without_a_live_holder(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    release_session_role(stores.conversations, stores.projects, holder)
    worker = _session(stores)

    assert _claim(stores, worker)[1].session_id == worker


def test_a_taken_lane_name_is_refused(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    first, second = _session(stores), _session(stores)
    _claim(stores, first, by=holder)

    with pytest.raises(OmnigentError) as caught:
        _claim(stores, second, by=holder)
    assert caught.value.code == ErrorCode.CONFLICT
    with pytest.raises(OmnigentError) as caught:
        _claim(stores, first, "c31", by=holder)
    assert caught.value.code == ErrorCode.CONFLICT


def test_a_non_holder_cannot_assign_a_lane(stores: SimpleNamespace) -> None:
    _holder(stores)
    stranger, worker = _session(stores), _session(stores)

    with pytest.raises(OmnigentError) as caught:
        _claim(stores, worker, by=stranger)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert _get(stores, worker).labels.get(LANE_NAME_LABEL_KEY) is None


@pytest.mark.parametrize("lane", ["main", "*", "C30", "", "a" * 33, "c 30", None])
def test_bad_or_reserved_lane_names_are_refused(stores: SimpleNamespace, lane: object) -> None:
    holder = _holder(stores)

    with pytest.raises(OmnigentError) as caught:
        _claim(stores, _session(stores), lane, by=holder)
    assert caught.value.code == ErrorCode.INVALID_INPUT


def test_role_holders_and_unknown_roles_cannot_be_lanes(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    other_holder = _holder(stores, "Bench")

    for session in (holder, other_holder):
        with pytest.raises(OmnigentError) as caught:
            _claim(stores, session, by=holder)
        assert caught.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(OmnigentError) as caught:
        _claim(stores, _session(stores), role="Nobody")
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_closing_the_lane_session_frees_the_name(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker, successor = _session(stores), _session(stores)
    role, _ = _claim(stores, worker, by=holder)

    _close(stores, worker)

    assert stores.roles.find_lane(role.id, "c30") is None
    assert LANE_NAME_LABEL_KEY not in _get(stores, worker).labels
    assert _claim(stores, successor, by=holder)[1].session_id == successor
    assert _get(stores, holder).labels.get("omnigent.alias") == "Architect"


def test_a_stale_lane_whose_session_is_gone_is_reclaimed(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker, successor = _session(stores), _session(stores)
    _claim(stores, worker, by=holder)
    # Closed without the hook (e.g. a crash): the next claim repairs it.
    stores.conversations.set_labels(worker, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})

    assert _claim(stores, successor, by=holder)[1].session_id == successor


def test_release_and_holder_change_keep_the_lanes(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker = _session(stores)
    role, lane = _claim(stores, worker, by=holder)

    release_session_role(stores.conversations, stores.projects, holder)
    successor = _holder(stores)

    assert stores.roles.find_lane(role.id, "c30") == lane
    assert _get(stores, worker).labels[ROLE_LANE_LABEL_KEY] == "/Architect"
    # The new holder can assign lanes; the previous one no longer can.
    assert _claim(stores, _session(stores), "c31", by=successor)[1].name == "c31"
    with pytest.raises(OmnigentError) as caught:
        _claim(stores, _session(stores), "c32", by=holder)
    assert caught.value.code == ErrorCode.FORBIDDEN


def _resolve(stores: SimpleNamespace, address: str, source: str) -> AddressTarget:
    return resolve_address_target(
        stores.conversations,
        stores.projects,
        address=address,
        user_id=OWNER,
        source=_get(stores, source),
        default_namespace=ROOT,
    )


def test_a_lane_address_delivers_to_the_lane(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker, sender = _session(stores), _session(stores)
    _claim(stores, worker, by=holder)

    for address in ("Architect/c30", "/Architect/c30"):
        assert _resolve(stores, address, sender) == AddressTarget(
            worker, "/Architect/c30", "/Architect", "lane"
        )
    # Role addresses resolve exactly as before.
    assert _resolve(stores, "Architect", sender) == AddressTarget(holder)


def test_a_missing_or_closed_lane_falls_back_to_the_holder(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker, sender = _session(stores), _session(stores)
    _claim(stores, worker, by=holder)

    missing = _resolve(stores, "Architect/c99", sender)
    assert (missing.session_id, missing.delivered_to, missing.reason) == (
        holder,
        "holder",
        "lane c99 not found",
    )
    _close(stores, worker)
    closed = _resolve(stores, "Architect/c30", sender)
    assert (closed.session_id, closed.delivered_to) == (holder, "holder")


def test_a_lane_of_a_vacant_role_or_unknown_role_is_not_found(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    sender = _session(stores)
    release_session_role(stores.conversations, stores.projects, holder)

    for address in ("Architect/c30", "Nobody/c30", "Architect/main"):
        with pytest.raises(OmnigentError) as caught:
            _resolve(stores, address, sender)
        assert caught.value.code == ErrorCode.NOT_FOUND


def test_the_policy_route_names_the_lane_and_its_role(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    worker, sender = _session(stores), _session(stores)
    _claim(stores, worker, by=holder)
    resolve = messaging_route_resolver(stores.conversations, stores.projects)

    to_lane = resolve(sender, "Architect/c30", OWNER)
    assert to_lane is not None
    assert (to_lane.target, to_lane.role_target) == ("/Architect/c30", "/Architect")
    # The fallback keeps the lane name, so a lane rule still applies to it.
    assert resolve(sender, "Architect/c99", OWNER) == MessageRoute(
        to_lane.sender, "/Architect/c99", role_target="/Architect"
    )
    # Addressing the lane session by id is still a message to that lane.
    assert resolve(sender, worker, OWNER) == to_lane
    assert resolve(sender, "Architect", OWNER) == MessageRoute(to_lane.sender, "/Architect")


def _roles(stores: SimpleNamespace) -> dict[str, dict]:
    listed = list_namespace_roles(
        stores.conversations, stores.projects, namespace=ROOT, user_id=OWNER
    )
    return {entry["name"]: entry for entry in listed}


def test_the_role_list_shows_each_roles_lanes(stores: SimpleNamespace) -> None:
    holder = _holder(stores)
    _holder(stores, "Bench")
    worker, gone = _session(stores), _session(stores)
    _claim(stores, worker, by=holder, purpose="host flap", item="C30")
    _claim(stores, gone, "c31", by=holder)
    stores.conversations.set_labels(gone, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})

    listed = _roles(stores)

    assert listed["Bench"]["lanes"] == [] and listed["Bench"]["lanes_more"] == 0
    c30, c31 = listed["Architect"]["lanes"]
    assert {k: c30[k] for k in ("name", "session_id", "purpose", "item", "state", "stale")} == {
        "name": "c30",
        "session_id": worker,
        "purpose": "host flap",
        "item": "C30",
        "state": "open",
        "stale": False,
    }
    assert (c31["state"], c31["stale"]) == ("gone", True)


def test_the_role_list_caps_lanes_and_stays_small(stores: SimpleNamespace) -> None:
    for role in [f"Role{index}" for index in range(6)]:
        holder = _holder(stores, role)
        for index in range(22 if role == "Role5" else 10):
            _claim(
                stores,
                _session(stores),
                f"lane-{index:02d}",
                by=holder,
                role=role,
                purpose="measure the idle CPU of the hub after a restart",
                item="C30",
            )

    listed = _roles(stores)

    assert (len(listed["Role5"]["lanes"]), listed["Role5"]["lanes_more"]) == (20, 2)
    del listed["Role5"]
    tool_result = {"namespace": "/", "roles": compact_role_listing(list(listed.values()))}
    assert tool_result["roles"][0]["lanes"][0] == "lane-00 open C30"
    # Five roles of ten lanes fit the sys_role_list budget.
    assert len(json.dumps(tool_result)) < 4000
    (only,) = compact_role_listing(list(listed.values()), "role0")
    assert only["lanes"][0]["purpose"].startswith("measure")

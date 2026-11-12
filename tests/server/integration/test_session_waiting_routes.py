"""``/v1/sessions/waiting`` and ``/v1/sessions/{id}/waiting`` through the real app."""

import pytest

from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.role_threads import THREAD_ROLE_LABEL, thread_hash
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_permissions import _create_session_as

pytestmark = pytest.mark.asyncio

ALICE = {"X-Forwarded-Email": "alice"}


async def test_waiting_routes_report_status_approval_and_stay_owner_scoped(
    auth_client, auth_app, db_uri
):
    busy = await _create_session_as(auth_client, "", "alice")
    quiet = await _create_session_as(auth_client, "", "alice")
    former = await _create_session_as(auth_client, "", "alice")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(busy["id"], {ALIAS_LABEL_KEY: "Product"})
    # A former role holder still listed by its open thread, under the handle it held.
    digest = thread_hash("proj/Product")
    store.set_labels(
        former["id"],
        {
            THREAD_ROLE_LABEL: "proj/Architect",
            f"thread.{digest}.cp": "proj/Product",
            f"thread.{digest}.state": "waiting_on_owner",
            f"thread.{digest}.at": "1800000000",
        },
    )
    other = await _create_session_as(auth_client, "", "bob")
    _session_status_cache[busy["id"]] = "waiting"
    pending_elicitations.record_publish(
        busy["id"],
        {
            "type": "response.elicitation_request",
            "elicitation_id": "e1",
            "params": {"message": "Run it?"},
        },
    )
    try:
        one = await auth_client.get(f"/v1/sessions/{busy['id']}/waiting", headers=ALICE)
        assert one.status_code == 200
        body = one.json()
        assert body["session_id"] == busy["id"]
        assert body["status"] == "waiting"
        assert [(i["kind"], i["needs_owner"]) for i in body["items"]] == [("approval", True)]
        assert (body["project"], body["handle"], body["holds_role"]) == (None, "/Product", True)
        assert body["waiting_reason"] == "1 approval"

        listing = await auth_client.get("/v1/sessions/waiting", headers=ALICE)
        assert listing.status_code == 200
        ids = [s["session_id"] for s in listing.json()["sessions"]]
        assert busy["id"] in ids
        assert quiet["id"] not in ids
        assert other["id"] not in ids
        by_id = {s["session_id"]: s for s in listing.json()["sessions"]}
        shown = by_id[former["id"]]
        assert (shown["handle"], shown["holds_role"]) == ("proj/Architect", False)
        assert [i["needs_owner"] for i in shown["items"]] == [True]

        snapshot = await auth_client.get(f"/v1/sessions/{busy['id']}", headers=ALICE)
        assert snapshot.json()["status"] == "running"
        assert snapshot.json()["waiting_reason"] == "1 approval"
        assert "status_since" in snapshot.json()

        denied = await auth_client.get(
            f"/v1/sessions/{busy['id']}/waiting", headers={"X-Forwarded-Email": "bob"}
        )
        assert denied.status_code in (403, 404)
    finally:
        _session_status_cache.pop(busy["id"], None)
        pending_elicitations.reset_for_tests()
        session_live_state.forget_live_status(busy["id"])

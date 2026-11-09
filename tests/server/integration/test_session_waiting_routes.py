"""``/v1/sessions/waiting`` and ``/v1/sessions/{id}/waiting`` through the real app."""

import pytest

from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state
from omnigent.server.routes._sessions.common import _session_status_cache
from tests.server.integration.test_managed_startup_inputs import auth_app as auth_app
from tests.server.integration.test_managed_startup_inputs import (
    startup_permissions as startup_permissions,
)
from tests.server.integration.test_sessions_permissions import _create_session_as

pytestmark = pytest.mark.asyncio

ALICE = {"X-Forwarded-Email": "alice"}


async def test_waiting_routes_report_status_approval_and_stay_owner_scoped(auth_client, auth_app):
    busy = await _create_session_as(auth_client, "", "alice")
    quiet = await _create_session_as(auth_client, "", "alice")
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
        assert [i["kind"] for i in body["items"]] == ["approval"]
        assert body["waiting_reason"] == "1 approval"

        listing = await auth_client.get("/v1/sessions/waiting", headers=ALICE)
        assert listing.status_code == 200
        ids = [s["session_id"] for s in listing.json()["sessions"]]
        assert busy["id"] in ids
        assert quiet["id"] not in ids
        assert other["id"] not in ids

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

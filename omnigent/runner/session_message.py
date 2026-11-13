"""Adapt peer messages to the existing session event API."""

import json
import re
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import (
    is_wrong_replica,
    normalize_session_id,
    post_on_host,
    session_host_id,
)
from omnigent.tools.builtins.session_message import peer_session_messaging_enabled

# ``Role`` or ``project/Role``; anything else that is not a session id is rejected.
_ALIAS_ADDRESS = re.compile(r"(?:[A-Za-z][A-Za-z0-9._-]*/)?[A-Za-z][A-Za-z0-9_-]{0,31}")


async def execute_message_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    address = args.get("session_id")
    try:
        target: str | None = normalize_session_id(address)
    except ValueError:
        target = None  # not an id; resolved as an alias below
    message = args.get("message")
    if target is None:
        if not isinstance(address, str) or not _ALIAS_ADDRESS.fullmatch(address):
            return json.dumps({"error": "Provide a valid session_id (UUID or numeric managed ID)"})
        if client is None or source_id is None:
            return json.dumps({"error": "Session messaging requires an authenticated runner"})
        resolved = await _resolve_alias(client, source_id, address)
        if "error" in resolved:
            return json.dumps(resolved)
        target = resolved["session_id"]
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        return json.dumps({"error": "Provide a non-blank message of 1–8000 characters"})
    kind = args.get("kind", "action")
    if kind not in ("action", "fyi"):
        return json.dumps({"error": "kind must be 'action' or 'fyi'"})
    if client is None or source_id is None:
        return json.dumps({"error": "Session messaging requires an authenticated runner"})
    if not peer_session_messaging_enabled():
        return json.dumps({"error": "Messaging other sessions is disabled on this runner"})
    try:
        path = f"/v1/sessions/{target}/events"
        data: dict[str, Any] = {"message": message, "address": address}
        if kind == "fyi":
            data["kind"] = "fyi"
        payload = {"type": "session_message", "data": data}
        headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
        response = await client.post(path, headers=headers, json=payload, timeout=60)
        if is_wrong_replica(response):
            try:
                host_id = await session_host_id(client, target)
            except (httpx.HTTPError, ValueError):
                return json.dumps(
                    {"error": "Message not delivered: target host could not be resolved"}
                )
            response = await post_on_host(
                client, path, host_id=host_id, headers=headers, json=payload, timeout=60
            )
            if host_id and is_wrong_replica(response):
                # Older hosts may have registered on the default, unkeyed replica.
                response = await post_on_host(
                    client, path, host_id=None, headers=headers, json=payload, timeout=60
                )
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise httpx.HTTPError("Delivery acknowledgement unavailable")
    except (httpx.HTTPError, ValueError):
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
    if response.is_error:
        return json.dumps({"error": "Session message refused", "detail": response.text[:1000]})
    try:
        acknowledgement = response.json()
        if not isinstance(acknowledgement, dict) or not isinstance(
            acknowledgement.get("queued"), bool
        ):
            raise ValueError("Expected a delivery acknowledgement with boolean queued")
        if acknowledgement.get("denied") is True:
            acknowledgement["error"] = (
                f"Session message denied by policy: {acknowledgement.get('reason', 'Denied')}"
            )
        return json.dumps(acknowledgement)
    except ValueError:
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )


async def _resolve_alias(
    client: httpx.AsyncClient, source_id: str, address: str
) -> dict[str, Any]:
    """Resolve ``Role`` / ``project/Role`` relative to the sending session's project."""
    try:
        response = await client.get(
            "/v1/sessions/aliases/resolve",
            params={"address": address},
            headers={ORCHESTRATION_SOURCE_HEADER: source_id},
            timeout=30,
        )
    except httpx.HTTPError:
        return {"error": f"Could not resolve {address!r}; check sys_session_list"}
    if response.is_error:
        return {"error": f"Could not resolve {address!r}", "detail": response.text[:500]}
    try:
        body = response.json()
        return {"session_id": normalize_session_id(body["session_id"])}
    except (ValueError, KeyError, TypeError):
        return {"error": f"Could not resolve {address!r}"}

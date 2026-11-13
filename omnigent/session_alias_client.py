"""CLI-side calls for role aliases (``Role`` / ``project/Role``) on the Omnigent server."""

import os
import re
from typing import Any

import httpx

from omnigent.errors import ErrorCode, OmnigentError

# Set by the runner inside harness panes, so ``omnigent session alias Role`` can name
# the session it runs in.
_CURRENT_SESSION_ENV_VARS = ("OMNIGENT_SESSION_ID", "OMNIGENT_TERMINAL_LAUNCH_SESSION_ID")

ALIAS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def is_alias_address(address: str) -> bool:
    """Whether ``address`` has the shape of ``Role`` or ``project/Role``."""
    _, _, role = address.strip().rpartition("/")
    return bool(ALIAS_PATTERN.match(role))


def current_session_id() -> str | None:
    """The id of the session whose pane this process runs in, if any."""
    for name in _CURRENT_SESSION_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _checked(response: httpx.Response) -> Any:
    if response.is_error:
        try:
            message = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            message = response.text
        raise OmnigentError(
            message[:500] or f"HTTP {response.status_code}", code=ErrorCode.INVALID_INPUT
        )
    return response.json() if response.content else None


def list_aliases(client: httpx.Client, project: str | None = None) -> list[dict[str, Any]]:
    """Live aliased sessions the caller owns, optionally limited to one project name."""
    params = {"project": project} if project is not None else {}
    return list(_checked(client.get("/v1/sessions/aliases", params=params)).get("data", []))


def list_projects(client: httpx.Client) -> list[dict[str, Any]]:
    """The caller's first-class projects, with ``parent_id`` and computed ``path``."""
    return list(_checked(client.get("/v1/projects")).get("data", []))


def find_project(rows: list[dict[str, Any]], project: str) -> dict[str, Any]:
    """Pick the project whose id equals ``project`` or whose path matches it (any case)."""
    by_id = [row for row in rows if row.get("id") == project]
    if by_id:
        return by_id[0]
    wanted = project.strip("/").casefold()
    matches = [row for row in rows if str(row.get("path") or "").casefold() == wanted]
    if len(matches) != 1:
        problem = "No project" if not matches else "Several projects"
        raise OmnigentError(f"{problem} at {project!r}", code=ErrorCode.INVALID_INPUT)
    return matches[0]


def set_project_private(client: httpx.Client, project_id: str, private: bool) -> dict[str, Any]:
    """Set or clear the project's private-namespace flag (owner only)."""
    return dict(_checked(client.patch(f"/v1/projects/{project_id}", json={"private": private})))


def resolve_address(client: httpx.Client, address: str) -> dict[str, Any]:
    """Resolve a session id, ``project/Role``, ``/Role`` or a bare ``Role``.

    A bare ``Role`` is relative to the project of the working directory, else the root.
    """
    params = {"address": address}
    if "/" not in address:
        params["workspace"] = os.getcwd()
    return dict(_checked(client.get("/v1/sessions/aliases/resolve", params=params)))


def set_alias(client: httpx.Client, session_id: str, alias: str) -> dict[str, Any]:
    """Name a session; fails if the alias is already taken in its project."""
    return dict(_checked(client.put(f"/v1/sessions/{session_id}/alias", json={"alias": alias})))


def clear_alias(client: httpx.Client, session_id: str) -> None:
    """Remove a session's alias."""
    _checked(client.delete(f"/v1/sessions/{session_id}/alias"))


def send_message(client: httpx.Client, session_id: str, message: str) -> dict[str, Any]:
    """Queue an owner message as a normal user turn in the target session."""
    payload = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": message}]},
    }
    return dict(_checked(client.post(f"/v1/sessions/{session_id}/events", json=payload)) or {})


def reset_session(
    client: httpx.Client,
    session_id: str,
    *,
    stop_running: bool = False,
    drop_pending: bool = False,
    acknowledge_running_work: bool = False,
) -> dict[str, Any]:
    """Reset a session's model context in place via ``POST /v1/sessions/{id}/reset``.

    The body stays empty unless a flag is set, so a refusal uses the server's
    default words (a running turn, queued input, or live sub-agents and
    background work). ``stop_running``, ``drop_pending``, and
    ``acknowledge_running_work`` are the retries those words name.

    The reset can wait out a harness stop, so this call allows longer than the
    client's default timeout.

    :param client: Authenticated client for the Omnigent server.
    :param session_id: Session to reset, already resolved from an alias.
    :param stop_running: Interrupt a running turn before resetting.
    :param drop_pending: Discard queued input instead of refusing.
    :param acknowledge_running_work: Reset even if sub-agents or background
        work are still counted. The server's sentence names this field.
    :returns: The server's reset result (``session_id``, ``epoch``, ``boundary_item_id``).
    :raises OmnigentError: The server's message, unchanged, including a client timeout.
    """
    payload: dict[str, Any] = {}
    if stop_running:
        payload["stop_running"] = True
    if drop_pending:
        payload["pending"] = "drop"
    if acknowledge_running_work:
        payload["acknowledge_running_work"] = True
    try:
        response = client.post(
            f"/v1/sessions/{session_id}/reset",
            json=payload,
            timeout=90.0,
        )
    except httpx.TimeoutException as exc:
        raise OmnigentError(
            "The server did not finish the reset in time.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return dict(_checked(response) or {})

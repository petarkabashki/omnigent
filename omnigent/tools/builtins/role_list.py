"""Tool for listing the roles of a namespace."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysRoleListTool(Tool):
    """Schema-only tool the runner answers from ``GET /v1/sessions/aliases/roles``."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_role_list"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "List the roles of a namespace, held and vacant, with the handle to address each "
            "one by and the session holding it, plus its lanes (address one as 'Role/lane'). "
            "Defaults to your own namespace. Send to a held role with sys_session_message "
            "using its handle; a vacant role has no session."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": (
                                "Project path to list, e.g. 'forthkit/sites', or '/' for the "
                                "owner's unfiled roles. Omit for your own namespace."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "description": (
                                "List only this role, with each lane's session, purpose and "
                                "last activity. Omit to list every role with short lane lines."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }


def compact_role_listing(
    roles: list[dict[str, Any]], role: str | None = None
) -> list[dict[str, Any]]:
    """Shape the server's role rows for the tool result.

    Without ``role`` each lane becomes one ``"name state [stale] [item]"`` line so
    a busy namespace stays small; with ``role`` only that role is kept, in full.
    """
    if role is not None:
        return [
            entry for entry in roles if str(entry.get("name", "")).casefold() == role.casefold()
        ]
    return [
        {**entry, "lanes": [_lane_line(lane) for lane in entry.get("lanes", [])]}
        for entry in roles
    ]


def _lane_line(lane: dict[str, Any]) -> str:
    parts = [str(lane.get("name")), str(lane.get("state"))]
    if lane.get("stale") and lane.get("state") == "open":
        parts.append("stale")
    if lane.get("item"):
        parts.append(str(lane["item"]))
    return " ".join(parts)

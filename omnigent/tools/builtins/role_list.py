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
            "one by and the session holding it. Defaults to your own namespace. Send to a held "
            "role with sys_session_message using its handle; a vacant role has no session."
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
                        }
                    },
                    "additionalProperties": False,
                },
            },
        }

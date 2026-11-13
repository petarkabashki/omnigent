"""Tools for the calling session to take or release a role handle."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysRoleTakeTool(Tool):
    """Schema-only tool the runner forwards to ``PUT /v1/sessions/{id}/alias``."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_role_take"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Take a role handle for this session, in its own namespace, so other sessions can "
            "address it by that name. The owner is asked to approve every call; nothing changes "
            "unless they approve. Fails if another session holds the name or this session "
            "already holds a role (release it first)."
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
                        "role": {
                            "type": "string",
                            "description": "Role name to take, e.g. 'Architect'.",
                        }
                    },
                    "required": ["role"],
                    "additionalProperties": False,
                },
            },
        }


class SysRoleReleaseTool(Tool):
    """Schema-only tool the runner forwards to the alias DELETE or the role release POST."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_role_release"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "With no arguments, release the role handle this session holds, leaving the role "
            "vacant. With role and force=true, free a role another session holds (for example "
            "one whose runner is offline), optionally handing it to give_to. The owner is "
            "asked to approve every call; nothing changes unless they approve."
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
                        "role": {
                            "type": "string",
                            "description": "Role to force-release in this session's "
                            "namespace; needs force=true.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Required to release a role another session holds.",
                        },
                        "give_to": {
                            "type": "string",
                            "description": "Optional session id or role address to hand "
                            "the role to; it must be live, here, and hold no other role.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }

"""Agent requests to open workspace files in the current session's UI."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.tools.base import Tool


class OpenFileRequest(BaseModel):
    """A workspace-relative file to show, without reading or modifying it."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096, description="Workspace-relative file path.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        """Reject absolute paths, traversal, URLs and control characters."""
        parsed = PurePosixPath(path)
        if (
            not path.strip()
            or path.startswith(("/", "~"))
            or ".." in parsed.parts
            or not parsed.parts
            or path.endswith("/")
            or any(character in path for character in ("\\", ":"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise ValueError("path must name a workspace-relative file without traversal")
        return str(parsed)


class UiOpenFileTool(Tool):
    """Schema-only tool; the runner posts the request to the session API."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "ui_open_file"

    @classmethod
    def description(cls) -> str:
        """Return the agent-facing contract."""
        return (
            "Open a workspace-relative file in Omnigent UIs currently viewing this session. "
            "Does not modify the file or switch users to another session. Requests are not "
            "replayed later. The result reports stream delivery, not confirmation that a UI "
            "displayed the file; file access errors and unsaved-edit prompts remain in the UI."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": OpenFileRequest.model_json_schema(),
            },
        }


class UiRenderTool(Tool):
    """Schema-only tool; the runner validates the spec through ``/v1/views``."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "ui_render"

    @classmethod
    def description(cls) -> str:
        """Return the agent-facing contract."""
        return (
            "Show the user a live view of hub data (sessions, threads, roles or a sub-agent "
            "tree) as a List, Table or Tree in this conversation. Pass a view spec v1: "
            '{"spec": 1, "title": "...", "view": {"type": "Table", "source": {"name": '
            '"sessions", "params": {"archived": false}}, "columns": [{"header": "Session", '
            '"value": "$.title", "link": "$.hub_ref"}]}}. Bind row fields with "$.field"; '
            'decide badges and marks with JSONLogic rules such as {"==": [{"var": "stopped"}, '
            "true]}. The view is resolved for the user who reads it, so it stays current. "
            "Returns the normalized spec and how many rows it matches now, or why it is invalid."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format tool schema; the full spec schema is enforced server-side."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "spec": {
                            "type": "object",
                            "description": (
                                "View spec v1 (omnigent/views/spec_v1.schema.json): spec, "
                                "title, view{type List|Table|Tree, source{name sessions|"
                                "threads|roles|subagents, params, order, limit}, item or "
                                "columns, marks, empty}."
                            ),
                        }
                    },
                    "required": ["spec"],
                    "additionalProperties": False,
                },
            },
        }

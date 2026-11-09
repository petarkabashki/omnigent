"""Tools for the role-thread index: list, read history, and note state."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_STATES = ["open", "waiting_on_them", "waiting_on_us", "blocked", "done"]


class _ThreadTool(Tool):
    """Schema-only tool the runner answers from ``/v1/sessions/threads``."""

    _PROPERTIES: dict[str, Any] = {}
    _REQUIRED: list[str] = []

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": self._PROPERTIES,
            "additionalProperties": False,
        }
        if self._REQUIRED:
            parameters["required"] = self._REQUIRED
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": parameters,
            },
        }


class SysThreadListTool(_ThreadTool):
    """List the role's threads from the hub index."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_thread_list"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "List your role's conversation threads, one per counterpart, from the hub's "
            "index (not from your context, so it is intact after compaction). Each row has "
            "counterpart, state, last_message_id, unread, messages, summary and "
            "promotion_candidate (a long thread worth moving to its own side chat). Blocked "
            "and waiting_on_us threads come first. Incoming messages carry a "
            "'[thread <counterpart>]' tag."
        )


class SysThreadHistoryTool(_ThreadTool):
    """Read one thread's messages across every session that held the role."""

    _PROPERTIES = {
        "counterpart": {
            "type": "string",
            "description": "Thread key from sys_thread_list, e.g. 'project/Role'.",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    }
    _REQUIRED = ["counterpart"]

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_thread_history"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Read the messages received on one thread, oldest first, from every session "
            "that held your role, so history follows the role rather than the session. "
            "Returns the newest 'limit' (default 50) with session_id, message_id, "
            "created_at and text."
        )


class SysThreadNoteTool(_ThreadTool):
    """Set a thread's state or summary; a thread side chat also notifies its holder."""

    _PROPERTIES = {
        "counterpart": {
            "type": "string",
            "description": "Thread key. Omit inside a thread side chat; it is implied.",
        },
        "state": {"type": "string", "enum": _STATES},
        "summary": {
            "type": "string",
            "maxLength": 2000,
            "description": "Current state of the thread; the index keeps the first 256 chars.",
        },
    }

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_thread_note"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Record a thread's state and/or one-line summary in the role's index. In a side "
            "chat opened for a thread, also sends the role's main session a "
            "'[thread note]' summary line; call it after any commitment, dependency, "
            "conflict or blocker."
        )

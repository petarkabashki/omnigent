"""Tools for role and group threads: list, read history, note state and post."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

_STATES = ["open", "waiting_on_them", "waiting_on_us", "waiting_on_owner", "blocked", "done"]


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
            "promotion_candidate (a long thread worth moving to its own side chat). "
            "waiting_on_owner, blocked and waiting_on_us threads come first. Incoming messages "
            "carry a '[thread <counterpart>]' tag; '[Message from owner ...]' is the owner "
            "speaking in that thread, and both roles in it may see it. group_threads lists "
            "the group threads (keys 'grp_…') you are a member of."
        )


class SysThreadHistoryTool(_ThreadTool):
    """Read one thread's messages across every session that held the role."""

    _PROPERTIES = {
        "counterpart": {
            "type": "string",
            "description": "Thread key from sys_thread_list, e.g. 'project/Role' or 'grp_…'.",
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
            "Read both sides of one thread, oldest first, from every session "
            "that held your role, so history follows the role rather than the session. "
            "Returns the newest 'limit' (default 50) with direction (in, out or owner), "
            "session_id, message_id, created_at and text. For a group thread key ('grp_…') "
            "returns its posts with seq, author and text."
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
            "Record a thread's state and/or one-line summary in the role's index; this also "
            "marks the thread read. In a side "
            "chat opened for a thread, also sends the role's main session a "
            "'[thread note]' summary line; call it after any commitment, dependency, "
            "conflict or blocker. Set state 'waiting_on_owner' when the thread needs the "
            "owner's decision; the owner's next post in the thread clears it."
        )


class SysThreadPostTool(_ThreadTool):
    """Post into a group thread; only the members named in ``to`` get a turn."""

    _PROPERTIES = {
        "thread": {"type": "string", "description": "Group thread key, 'grp_…'."},
        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
        "to": {
            "description": (
                "Members who should answer: a list of their refs ('project/Role' or a "
                "session id), or 'everyone'. Omit to post without asking anyone to reply."
            ),
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string", "enum": ["everyone"]},
            ],
        },
        "summary": {
            "type": "string",
            "maxLength": 2000,
            "description": "Optional new summary of the whole thread, shown to late joiners.",
        },
    }
    _REQUIRED = ["thread", "message"]

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_thread_post"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Post into a group thread you are a member of. Every member can read the post "
            "with sys_thread_history; only those named in 'to' get a turn to answer. After "
            "six agent posts without the owner posting, posts are refused: stop and ask the "
            "owner."
        )

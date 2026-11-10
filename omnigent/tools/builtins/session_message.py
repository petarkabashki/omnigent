"""Ordinary messages between independent sessions owned by the same user."""

from typing import Any

from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.tools.base import Tool


def peer_session_messaging_enabled() -> bool:
    """Whether this process's ``OMNIGENT_FEATURES`` leaves peer messaging enabled."""
    return resolve_feature_flags().enabled(Feature.PEER_SESSION_MESSAGING)


class SysSessionMessageTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_message"

    @classmethod
    def description(cls) -> str:
        return (
            "Send work to another session owned by the same user, by session ID or by "
            "alias: 'Role' (your project), 'project/Role' (e.g. 'py-qwant/Architect') or '/Role' "
            "(the owner's unfiled roles); sys_role_list shows what exists. "
            "Messages you receive start with the sender's handle; reply to it by name. "
            "The result includes message_id (msg_<hex>), which the receiver sees in the header "
            "'[Message from <handle> (session <id>, message <msg_id>)]', useful for threading "
            "replies. "
            "Requires session "
            "orchestration enabled and a sending session that is private or shared "
            "read-only, including its side-chat source chain. The receiver "
            "may be shared: its readers will see your message. It uses normal queue/steering "
            "behavior without changing parentage or forwarding replies. A queued result "
            "means accepted, not executed; destination policy may still deny it during "
            "startup replay. Check results with "
            "sys_session_get_info/sys_session_get_history. Do not automatically retry an "
            "uncertain delivery."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Destination session ID, 'Role', 'project/Role' or '/Role'."
                            ),
                        },
                        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                    },
                    "required": ["session_id", "message"],
                    "additionalProperties": False,
                },
            },
        }

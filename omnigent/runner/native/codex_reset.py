"""Codex-native context reset: stop the pane, forget its thread, relaunch fresh.

Codex shares the claude-native reset phases (:class:`ClaudeNativeReset`). The
server clears ``external_session_id`` and the fork labels between them, so the
relaunch reaches the fresh-thread branch of ``_auto_create_codex_terminal`` and
resolves the agent spec again, which re-applies the session's role overlay.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native.forwarder import reset_codex_forward_state
from omnigent.runner.turn_routing import clear_pending_replay


def wipe_codex_resume_state(bridge_dir: Path) -> None:
    """Remove the local state that would replay the pre-reset conversation.

    :param bridge_dir: The session's Codex bridge directory.
    """
    reset_codex_forward_state(bridge_dir)
    # A routed prompt a previous launch blocked belongs before the boundary.
    clear_pending_replay(bridge_dir)

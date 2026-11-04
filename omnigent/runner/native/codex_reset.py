"""Codex-native context reset: stop the pane, forget its thread, relaunch fresh.

Codex shares the claude-native reset phases (:class:`ClaudeNativeReset`). The
server clears ``external_session_id`` and the fork labels between them, so the
relaunch reaches the fresh-thread branch of ``_auto_create_codex_terminal`` and
resolves the agent spec again, which re-applies the session's role overlay.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.claude_native.bridge import read_launch_epoch, record_launch_epoch
from omnigent.harnesses.codex_native.forwarder import reset_codex_forward_state
from omnigent.runner.turn_routing import clear_pending_replay


def wipe_codex_resume_state(bridge_dir: Path) -> None:
    """Remove the local state that would replay the pre-reset conversation.

    Dead letters stay: this runs before the boundary exists and the file also holds
    letters of the session this one replaced. Replay compares their epochs instead.

    :param bridge_dir: The session's Codex bridge directory.
    """
    reset_codex_forward_state(bridge_dir)
    # A routed prompt a previous launch blocked belongs before the boundary.
    clear_pending_replay(bridge_dir)


def stamp_codex_launch_epoch(
    bridge_dir: Path, context_epoch: int, *, thread_id: str | None
) -> None:
    """Stamp a launch's epoch, catching up on a reset no runner ran.

    A reset with no bound runner never wipes the bridge dir, so the first launch
    after it sees the boundary as an older stamp and a cleared thread id.

    :param bridge_dir: The session's Codex bridge directory.
    :param context_epoch: The session's current context epoch.
    :param thread_id: The Codex thread this launch resumes, if any.
    """
    if thread_id is None and (read_launch_epoch(bridge_dir) or 0) < context_epoch:
        wipe_codex_resume_state(bridge_dir)
    record_launch_epoch(bridge_dir, context_epoch)

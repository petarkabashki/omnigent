"""Claude-native context reset: stop the pane, forget its Claude session, relaunch fresh.

The server persists the ``context_reset`` boundary and clears the session's
``external_session_id`` between the two runner phases, so the relaunch finds no
id to ``--resume`` and Claude starts a new conversation on the same Omnigent
session. The relaunch resolves the agent spec again, which re-applies the
session's role overlay to ``--append-system-prompt``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.harnesses.claude_native.bridge import (
    clear_claude_session_state,
    clear_claude_status_snapshot,
    read_launch_epoch,
)
from omnigent.harnesses.claude_native.forwarder import reset_transcript_forward_state


def claude_launch_bridge_id(session_id: str, labelled_bridge_id: str | None) -> str:
    """Return the bridge id a Claude launch for ``session_id`` uses.

    A session superseded by ``/clear`` keeps its own ``"{id}-cleared"`` bridge,
    because the replacement session owns ``D(session_id)``; every other session
    launches in ``D(session_id)``.

    :param session_id: Omnigent session id, e.g. ``"conv_abc"``.
    :param labelled_bridge_id: The session's bridge id label, if any.
    :returns: The bridge id to launch and reset under.
    """
    cleared = f"{session_id}-cleared"
    return cleared if labelled_bridge_id == cleared else session_id


def local_resume_hint_epoch(bridge_dir: Path) -> int:
    """Return the context epoch the bridge dir's Claude session hint belongs to.

    :param bridge_dir: The session's Claude bridge directory.
    :returns: The stamped epoch, or ``0`` when no launch stamped one.
    """
    return read_launch_epoch(bridge_dir) or 0


def wipe_claude_resume_state(bridge_dir: Path) -> None:
    """Remove the local hints that would make the next launch resume the old chat.

    :param bridge_dir: The session's Claude bridge directory.
    """
    clear_claude_session_state(bridge_dir)
    clear_claude_status_snapshot(bridge_dir)
    reset_transcript_forward_state(bridge_dir)


#: How long ``prepare`` keeps launches out when no ``commit`` follows.
RESET_ENSURE_HOLD_S = 90.0
#: How long ``prepare`` waits for a running launch. Stopping the pane must still
#: fit the server's forward budget: a late stop would apply a reset it reported failed.
RESET_PREPARE_LOCK_WAIT_S = 10.0


class ClaudeResetBusyError(Exception):
    """A launch held the session's Claude ensure lock past the reset's wait."""


@dataclass
class _EnsureHold:
    lock: asyncio.Lock
    expiry: asyncio.TimerHandle | None = None


@dataclass(frozen=True)
class ClaudeNativeReset:
    """The runner's two reset phases for a claude-native session.

    ``prepare`` takes the session's Claude ensure lock and keeps it until
    ``commit``, so no launch in between resumes the old Claude session.

    :param stop_pane: Closes the Claude pane and its transcript forwarder, so
        no late hook re-records the old Claude session id.
    :param bridge_dir_for_session: Resolves the session's bridge directory.
    :param relaunch: Starts a fresh Claude pane for the session.
    :param ensure_lock_for_session: Returns the lock every Claude launch takes.
    :param hold_s: Seconds after which an uncommitted ``prepare`` lets launches
        run again.
    """

    stop_pane: Callable[[str], Awaitable[None]]
    bridge_dir_for_session: Callable[[str], Awaitable[Path]]
    relaunch: Callable[[str], Awaitable[None]]
    ensure_lock_for_session: Callable[[str], asyncio.Lock]
    hold_s: float = RESET_ENSURE_HOLD_S
    _holds: dict[str, _EnsureHold] = field(default_factory=dict)

    async def prepare(self, session_id: str) -> None:
        """Stop the pane and hold launches off until ``commit``.

        :raises ClaudeResetBusyError: A launch outlasted the wait; nothing changed.
        """
        # A previous prepare whose commit never came must not block this one.
        self._release(session_id)
        lock = self.ensure_lock_for_session(session_id)
        try:
            async with asyncio.timeout(RESET_PREPARE_LOCK_WAIT_S):
                await lock.acquire()
        except TimeoutError as exc:
            raise ClaudeResetBusyError(
                "The session's Claude terminal is still starting; retry the reset shortly."
            ) from exc
        try:
            await self.stop_pane(session_id)
            await self._wipe(session_id)
        except BaseException:
            lock.release()
            raise
        hold = _EnsureHold(lock)
        hold.expiry = asyncio.get_running_loop().call_later(
            self.hold_s, self._expire, session_id, hold
        )
        self._holds[session_id] = hold

    async def commit(self, session_id: str) -> None:
        """Relaunch after the server cleared the session's Claude session id."""
        hold = self._holds.pop(session_id, None)
        if hold is None:
            lock = self.ensure_lock_for_session(session_id)
            await lock.acquire()
        else:
            if hold.expiry is not None:
                hold.expiry.cancel()
            lock = hold.lock
        try:
            # A pane launched after an expired hold still resumes the old chat,
            # and a hook that outlived the pane may have rewritten the hint.
            await self.stop_pane(session_id)
            await self._wipe(session_id)
        finally:
            lock.release()
        await self.relaunch(session_id)

    def _release(self, session_id: str) -> None:
        hold = self._holds.pop(session_id, None)
        if hold is None:
            return
        if hold.expiry is not None:
            hold.expiry.cancel()
        hold.lock.release()

    def _expire(self, session_id: str, hold: _EnsureHold) -> None:
        if self._holds.get(session_id) is hold:
            self._release(session_id)

    async def _wipe(self, session_id: str) -> None:
        bridge_dir = await self.bridge_dir_for_session(session_id)
        await asyncio.to_thread(wipe_claude_resume_state, bridge_dir)

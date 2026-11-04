"""Claude-native context reset: stop the pane, forget its Claude session, relaunch fresh.

The server persists the ``context_reset`` boundary and clears the session's
``external_session_id`` between the two runner phases, so the relaunch finds no
id to ``--resume`` and Claude starts a new conversation on the same Omnigent
session. The relaunch resolves the agent spec again, which re-applies the
session's role overlay to ``--append-system-prompt``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.harnesses.claude_native.bridge import (
    clear_claude_session_state,
    clear_claude_status_snapshot,
    read_launch_epoch,
)
from omnigent.harnesses.claude_native.forwarder import reset_transcript_forward_state

_logger = logging.getLogger(__name__)


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
    """A launch held the session's ensure lock past the reset's wait."""


@dataclass
class _EnsureHold:
    bridge_dir: Path
    lock: asyncio.Lock
    expiry: asyncio.TimerHandle | None = None
    expired: bool = False


@dataclass(frozen=True)
class ClaudeNativeReset:
    """The runner's two reset phases for a native terminal session.

    Claude and codex-native share these phases. ``prepare`` takes the session's
    ensure lock and keeps it until ``commit``, so no launch in between resumes
    the old vendor session. ``prepare`` resolves the bridge directory once, so
    ``commit`` has no lookup left to fail after the boundary is written.

    :param stop_pane: Closes the session's pane and its transcript forwarder, so
        no late hook re-records the old Claude session id. Takes the session id
        and its bridge directory.
    :param bridge_dir_for_session: Resolves the session's bridge directory.
    :param relaunch: Starts a fresh Claude pane for the session.
    :param ensure_lock_for_session: Returns the lock every Claude launch takes.
    :param hold_s: Seconds after which an uncommitted ``prepare`` lets launches
        run again.
    :param wipe: Removes the bridge's local resume and forward-cursor state.
    :param runtime_label: Vendor name for the busy refusal, e.g. ``"Codex"``.
    """

    stop_pane: Callable[[str, Path], Awaitable[None]]
    bridge_dir_for_session: Callable[[str], Awaitable[Path]]
    relaunch: Callable[[str], Awaitable[None]]
    ensure_lock_for_session: Callable[[str], asyncio.Lock]
    hold_s: float = RESET_ENSURE_HOLD_S
    wipe: Callable[[Path], None] = wipe_claude_resume_state
    runtime_label: str = "Claude"
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
                f"{self.runtime_label} is still starting. Try the reset again in a moment."
            ) from exc
        try:
            bridge_dir = await self.bridge_dir_for_session(session_id)
            await self.stop_pane(session_id, bridge_dir)
            await self._wipe(bridge_dir)
        except BaseException:
            lock.release()
            raise
        hold = _EnsureHold(bridge_dir, lock)
        hold.expiry = asyncio.get_running_loop().call_later(
            self.hold_s, self._expire, session_id, hold
        )
        self._holds[session_id] = hold

    async def commit(self, session_id: str) -> None:
        """Relaunch after the server cleared the session's Claude session id."""
        hold = self._holds.pop(session_id, None)
        if hold is None or hold.expired:
            lock = self.ensure_lock_for_session(session_id)
            await lock.acquire()
        else:
            if hold.expiry is not None:
                hold.expiry.cancel()
            lock = hold.lock
        try:
            bridge_dir = hold.bridge_dir if hold is not None else None
            if bridge_dir is None:
                bridge_dir = await self._bridge_dir_for_unprepared_commit(session_id)
            if bridge_dir is not None:
                # A pane launched after an expired hold still resumes the old chat,
                # and a hook that outlived the pane may have rewritten the hint.
                await self.stop_pane(session_id, bridge_dir)
                await self._wipe(bridge_dir)
        finally:
            lock.release()
        await self.relaunch(session_id)

    async def _bridge_dir_for_unprepared_commit(self, session_id: str) -> Path | None:
        """Resolve the directory for a ``commit`` this runner saw no ``prepare`` for.

        The boundary is already written, so a failed lookup must still end in a
        relaunch; the launch's own checks drop the stale state then.
        """
        try:
            return await self.bridge_dir_for_session(session_id)
        except ClaudeResetBusyError:
            _logger.warning(
                "Relaunching %s without a stop or wipe: its bridge directory is unknown",
                session_id,
                extra={"session_id": session_id},
            )
            return None

    def _release(self, session_id: str) -> None:
        hold = self._holds.pop(session_id, None)
        if hold is None or hold.expired:
            return
        if hold.expiry is not None:
            hold.expiry.cancel()
        hold.lock.release()

    def _expire(self, session_id: str, hold: _EnsureHold) -> None:
        # Stays recorded: a late ``commit`` still acts on the directory it resolved.
        if self._holds.get(session_id) is hold:
            hold.expired = True
            hold.lock.release()

    async def _wipe(self, bridge_dir: Path) -> None:
        await asyncio.to_thread(self.wipe, bridge_dir)

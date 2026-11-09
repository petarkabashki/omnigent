"""Hub-side checker that keeps role threads from going stale.

A thread with unhandled messages on an idle role holder gets up to a few
reminder messages; after that, or when the holder's runner is offline, it is
marked ``waiting_on_owner`` so the owner sees it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from omnigent.entities import Conversation
from omnigent.server import role_threads
from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

NUDGES_FIELD = "nudges"
NUDGED_AT_FIELD = "nudged_at"
HUB_SENDER = role_threads.HUB_SENDER
_PAGE_LIMIT = 100
_MAX_PAGES = 20

Deliver = Callable[[Conversation, str], Awaitable[None]]


@dataclass(frozen=True)
class ReminderSettings:
    """Checker settings; ``minutes == 0`` disables it."""

    minutes: int = 20
    max_nudges: int = 2
    interval_seconds: int = 120

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ReminderSettings:
        source = os.environ if env is None else env

        def read(name: str, default: int) -> int:
            try:
                return max(0, int(source.get(name, default)))
            except ValueError:
                return default

        return cls(
            minutes=read("OMNIGENT_THREAD_REMINDER_MINUTES", cls.minutes),
            max_nudges=read("OMNIGENT_THREAD_REMINDER_MAX", cls.max_nudges),
            interval_seconds=max(1, read("OMNIGENT_THREAD_REMINDER_INTERVAL_S", 120)),
        )


def reminder_text(counterpart: str, unread: int, age_minutes: int, now: int) -> str:
    """The reminder a holder receives; its thread tag shows it in the thread view."""
    noun = "message" if unread == 1 else "messages"
    return (
        f"[Message from {HUB_SENDER} (reminder {now})] [thread {counterpart}]\n"
        f"{unread} {noun} from {counterpart} unhandled for {age_minutes} min. "
        "Reply or act on them, then record the thread with sys_thread_note."
    )


def _is_due(
    row: Mapping[str, Any], fields: Mapping[str, str], now: int, limit_seconds: int
) -> bool:
    if row["unread"] <= 0 or row["state"] in ("done", "waiting_on_owner"):
        return False
    if now - row["updated_at"] < limit_seconds:
        return False
    return now - role_threads._to_int(fields.get(NUDGED_AT_FIELD)) >= limit_seconds


async def check_holder(
    store: ConversationStore,
    holder: Conversation,
    *,
    settings: ReminderSettings,
    now: int,
    deliver: Deliver,
    runner_online: Callable[[str], bool],
) -> int:
    """Remind or escalate the holder's due threads; returns how many were touched."""
    limit_seconds = settings.minutes * 60
    touched = 0
    for row in role_threads.thread_index(holder.labels):
        counterpart = row["counterpart"]
        fields = role_threads._row_fields(holder.labels, counterpart)
        if not _is_due(row, fields, now, limit_seconds):
            continue
        label = lambda field, cp=counterpart: role_threads._label(cp, field)  # noqa: E731
        nudges = role_threads._to_int(fields.get(NUDGES_FIELD))
        if not runner_online(holder.id) or nudges >= settings.max_nudges:
            store.set_labels(holder.id, {label("state"): "waiting_on_owner"})
        elif holder.live_status == "idle":
            age = (now - row["updated_at"]) // 60
            await deliver(holder, reminder_text(counterpart, row["unread"], age, now))
            store.set_labels(
                holder.id,
                {label(NUDGES_FIELD): str(nudges + 1), label(NUDGED_AT_FIELD): str(now)},
            )
        else:
            continue
        touched += 1
    return touched


def _holders(store: ConversationStore) -> list[Conversation]:
    """Sessions that currently hold a role and carry thread labels."""
    holders: list[Conversation] = []
    after: str | None = None
    for _ in range(_MAX_PAGES):
        page = store.list_conversations(
            limit=_PAGE_LIMIT,
            after=after,
            has_label=role_threads.THREAD_ROLE_LABEL,
            kind=None,
        )
        holders.extend(session for session in page.data if role_threads.holds_role(session))
        if not page.has_more:
            break
        after = page.last_id
    return holders


async def check_all(
    store: ConversationStore,
    *,
    settings: ReminderSettings,
    deliver: Deliver,
    runner_online: Callable[[str], bool],
    clock: Callable[[], float] = time.time,
) -> int:
    """One pass over every role holder; a failure on one holder doesn't stop the rest."""
    touched = 0
    for holder in await asyncio.to_thread(_holders, store):
        try:
            touched += await check_holder(
                store,
                holder,
                settings=settings,
                now=int(clock()),
                deliver=deliver,
                runner_online=runner_online,
            )
        except Exception:
            _logger.warning("thread reminder failed for %s", holder.id, exc_info=True)
    return touched


async def run_checker(
    store: ConversationStore,
    *,
    settings: ReminderSettings,
    deliver: Deliver,
    runner_online: Callable[[str], bool],
) -> None:
    """Loop until cancelled."""
    while True:
        await asyncio.sleep(settings.interval_seconds)
        with suppress(Exception):
            await check_all(store, settings=settings, deliver=deliver, runner_online=runner_online)


def start_checker(
    app: Any,
    store: ConversationStore,
    *,
    runner_online: Callable[[str], bool],
    user_id: str | None,
    settings: ReminderSettings | None = None,
) -> asyncio.Task[None] | None:
    """Start the background checker, or return None when disabled."""
    settings = settings or ReminderSettings.from_env()
    if settings.minutes <= 0:
        return None

    async def deliver(holder: Conversation, text: str) -> None:
        from starlette.datastructures import Headers

        from omnigent.server.routes.sessions.routes_events import _RunnerEventContext
        from omnigent.server.schemas import SessionEventInput

        dispatch = getattr(getattr(app.state, "startup_inputs", None), "dispatch", None)
        if dispatch is None or holder.runner_id is None:
            raise RuntimeError("no event dispatch available")
        # A hub-origin message rides the same path as a runner-posted one.
        context = _RunnerEventContext(app, Headers({}), user_id, holder.runner_id)
        data = {"role": "user", "content": [{"type": "input_text", "text": text}]}
        await dispatch(context, holder.id, SessionEventInput(type="message", data=data))

    return asyncio.create_task(
        run_checker(store, settings=settings, deliver=deliver, runner_online=runner_online),
        name="thread-reminders",
    )

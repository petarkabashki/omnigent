"""Thread reminders: due check, nudge, escalation, offline and reset."""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server import role_threads
from omnigent.server.thread_reminders import (
    ReminderSettings,
    check_all,
    reminder_text,
)
from omnigent.stores.conversation_store import ALIAS_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

NOW = 1_800_000_000
OLD = NOW - 30 * 60
SETTINGS = ReminderSettings(minutes=20, max_nudges=2, interval_seconds=1)


class Harness:
    def __init__(self, db_uri: str) -> None:
        self.store = SqlAlchemyConversationStore(db_uri)
        self.sent: list[tuple[str, str]] = []
        self.online = True
        self.offline_since: dict[str, int] = {}
        self.holder_id = self.store.create_conversation(kind="default").id
        self.store.set_session_live_status(self.holder_id, "idle")
        self.store.set_labels(
            self.holder_id,
            {ALIAS_LABEL_KEY: "Architect", role_threads.THREAD_ROLE_LABEL: "Architect"},
        )

    def thread(
        self, counterpart: str = "Product", *, unread: int = 2, at: int = OLD, **extra: str
    ):
        fields = {
            "cp": counterpart,
            "state": "waiting_on_us",
            "unread": str(unread),
            "at": str(at),
        }
        labels = {role_threads._label(counterpart, k): v for k, v in {**fields, **extra}.items()}
        self.store.set_labels(self.holder_id, labels)

    def row(self, counterpart: str = "Product") -> dict[str, str]:
        labels = self.store.get_conversation(self.holder_id).labels  # type: ignore[union-attr]
        return role_threads._row_fields(labels, counterpart)

    async def deliver(self, holder, text: str) -> None:
        self.sent.append((holder.id, text))
        data = MessageData(role="user", content=[{"type": "input_text", "text": text}])
        self.store.append(
            holder.id, [NewConversationItem(type="message", response_id="r", data=data)]
        )

    async def check(self, now: int = NOW) -> int:
        return await check_all(
            self.store,
            settings=SETTINGS,
            deliver=self.deliver,
            runner_online=lambda _sid: self.online,
            clock=lambda: now,
            offline_since=self.offline_since,
        )


@pytest.fixture
def harness(db_uri: str) -> Harness:
    return Harness(db_uri)


async def test_stale_unread_thread_gets_one_reminder(harness: Harness) -> None:
    harness.thread()
    assert await harness.check() == 1
    [(session_id, text)] = harness.sent
    assert session_id == harness.holder_id
    assert text == reminder_text("Product", 2, 30, NOW)
    assert text.startswith(
        f"[Message from hub (reminder {NOW})] [thread Product]\n2 messages from"
    )
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    view = role_threads.thread_conversation(
        harness.store, holder, "Product", user_id=None, limit=10
    )
    assert [row["direction"] for row in view] == ["hub"]  # shown in the thread view
    assert harness.row()["nudges"] == "1" and harness.row()["nudged_at"] == str(NOW)
    assert harness.row()["unread"] == "2"  # the reminder is not an unread message
    assert await harness.check(NOW + 60) == 0  # not nudged again within the limit


@pytest.mark.parametrize(
    "extra",
    [
        {"unread": 0},
        {"state": "done"},
        {"at": NOW - 5 * 60},
        {"nudged_at": str(NOW - 5 * 60)},
    ],
)
async def test_threads_not_due_are_left_alone(harness: Harness, extra: dict) -> None:
    kwargs = {k: v for k, v in extra.items() if k in ("unread", "at")}
    labels = {k: v for k, v in extra.items() if k not in ("unread", "at")}
    harness.thread(**kwargs, **labels)
    if "state" in labels:
        harness.thread(unread=2, state="done")
    assert await harness.check() == 0
    assert harness.sent == []


async def test_busy_holder_is_not_interrupted(harness: Harness) -> None:
    harness.thread()
    harness.store.set_session_live_status(harness.holder_id, "running")
    assert await harness.check() == 0
    assert harness.sent == [] and "nudges" not in harness.row()


async def test_after_max_nudges_thread_shows_no_reply_not_the_owner(harness: Harness) -> None:
    harness.thread(nudges="2", nudged_at=str(OLD))
    assert await harness.check() == 1
    assert harness.sent == []
    assert harness.row()["state"] == role_threads.NO_REPLY_STATE
    assert await harness.check(NOW + 3600) == 0  # escalated threads aren't revisited


async def test_offline_runner_escalates_only_after_the_reminder_window(harness: Harness) -> None:
    harness.thread(sum="please review")
    harness.online = False
    assert await harness.check() == 0  # first seen offline now
    assert await harness.check(NOW + 19 * 60) == 0
    assert harness.row()["state"] == "waiting_on_us"
    assert await harness.check(NOW + 20 * 60) == 1
    assert harness.sent == []
    assert harness.row()["state"] == role_threads.NO_REPLY_STATE
    assert harness.row()["sum"] == "please review"


async def test_a_hub_restart_alone_never_escalates(harness: Harness) -> None:
    harness.thread()
    harness.online = False
    await harness.check()
    harness.offline_since = {}  # a restarted hub has no memory of when the runner left
    assert await harness.check(NOW + 30 * 60) == 0
    assert harness.row()["state"] == "waiting_on_us"


async def test_a_runner_back_online_restarts_its_offline_clock(harness: Harness) -> None:
    harness.thread()
    harness.online = False
    await harness.check()
    harness.online = True
    harness.store.set_session_live_status(harness.holder_id, "running")
    await harness.check(NOW + 10 * 60)
    harness.online = False
    assert await harness.check(NOW + 25 * 60) == 0
    assert harness.row()["state"] == "waiting_on_us"


@pytest.mark.parametrize("state", ["waiting_on_owner", role_threads.NO_REPLY_STATE])
async def test_an_owner_post_takes_the_thread_off_the_owner(harness: Harness, state: str) -> None:
    harness.thread(state=state)
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    role_threads.settle_owner_wait(harness.store, holder, "Product")
    assert harness.row()["state"] == "open"


async def test_a_new_state_from_the_role_replaces_no_reply(harness: Harness) -> None:
    harness.thread(state=role_threads.NO_REPLY_STATE)
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    role_threads.note_thread(harness.store, holder, "Product", state="blocked", summary=None)
    assert harness.row()["state"] == "blocked"


def test_roles_cannot_set_no_reply(harness: Harness) -> None:
    harness.thread()
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    with pytest.raises(OmnigentError, match="state must be one of"):
        role_threads.note_thread(
            harness.store, holder, "Product", state=role_threads.NO_REPLY_STATE, summary=None
        )


async def test_a_held_thread_stays_on_the_owner_through_new_messages(harness: Harness) -> None:
    harness.thread(state="open", unread=0)
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    role_threads.hold_message(
        harness.store,
        holder,
        "Product",
        text="ok 6",
        target_id="target",
        source_id=holder.id,
        message_id=None,
        sender_handle="Architect",
    )
    role_threads.record_inbound(harness.store, holder, "Product", "Architect", "m1", "hi", 1)
    assert harness.row()["state"] == "waiting_on_owner"
    assert harness.row()["unread"] == "1"
    role_threads.settle_owner_wait(harness.store, holder, "Product")  # held still unresolved
    assert harness.row()["state"] == "waiting_on_owner"


async def test_delivery_failure_does_not_count_a_nudge(harness: Harness) -> None:
    harness.thread()

    async def broken(_holder, _text) -> None:
        raise RuntimeError("no dispatch")

    await check_all(
        harness.store,
        settings=SETTINGS,
        deliver=broken,
        runner_online=lambda _sid: True,
        clock=lambda: NOW,
    )
    assert "nudges" not in harness.row()


async def test_handling_a_thread_resets_the_reminder_count(harness: Harness) -> None:
    harness.thread(nudges="2", nudged_at=str(OLD))
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    role_threads.note_thread(harness.store, holder, "Product", state="open", summary=None)
    assert harness.row()["nudges"] == "0" and harness.row()["nudged_at"] == "0"

    harness.thread(nudges="1", nudged_at=str(OLD))
    role_threads.record_outbound(harness.store, holder, "Product", "Architect", None, "ok", 1)
    assert harness.row()["nudges"] == "0" and harness.row()["nudged_at"] == "0"


def test_settings_read_env_and_zero_disables() -> None:
    settings = ReminderSettings.from_env(
        {"OMNIGENT_THREAD_REMINDER_MINUTES": "0", "OMNIGENT_THREAD_REMINDER_MAX": "5"}
    )
    assert (settings.minutes, settings.max_nudges, settings.interval_seconds) == (0, 5, 120)
    assert ReminderSettings.from_env({}) == ReminderSettings()


@pytest.mark.parametrize("state", ["waiting_on_owner", role_threads.NO_REPLY_STATE])
async def test_a_new_message_restarts_the_reminder_sequence(harness: Harness, state: str) -> None:
    harness.thread(state=state, nudges="2", nudged_at=str(OLD))
    holder = harness.store.get_conversation(harness.holder_id)
    assert holder is not None
    role_threads.record_inbound(
        harness.store, holder, "Product", "Architect", "m1", "again", 1, now=OLD
    )
    assert harness.row()["state"] == "waiting_on_us"
    assert harness.row()["nudges"] == "0" and harness.row()["nudged_at"] == "0"
    assert await harness.check() == 1
    assert len(harness.sent) == 1  # reminded, not escalated
    assert harness.row()["state"] == "waiting_on_us"


@pytest.mark.parametrize("nudges", ["0", "2"])
async def test_a_blocked_thread_is_neither_nudged_nor_escalated(
    harness: Harness, nudges: str
) -> None:
    harness.thread(state="blocked", nudges=nudges, nudged_at=str(OLD))
    assert await harness.check() == 0
    assert harness.sent == []
    assert harness.row()["state"] == "blocked"

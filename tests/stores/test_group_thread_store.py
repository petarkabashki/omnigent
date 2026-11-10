"""Tests for :class:`SqlAlchemyGroupThreadStore`: owner scoping, members, seq and the hop cap."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.stores.group_thread_store import (
    GroupThreadLoopGuardError,
    SqlAlchemyGroupThreadStore,
)

OWNER = "alice"


@pytest.fixture()
def threads(db_uri: str) -> SqlAlchemyGroupThreadStore:
    return SqlAlchemyGroupThreadStore(db_uri)


def _post(
    threads: SqlAlchemyGroupThreadStore, thread_id: str, author_kind: str, *, max_hops: int = 2
) -> int:
    return threads.append_post(
        thread_id,
        user_id=OWNER,
        author_kind=author_kind,  # type: ignore[arg-type]
        author_ref="owner" if author_kind == "owner" else "omnigent/Product",
        text="hello",
        mentions=[],
        max_hops=max_hops,
        hop_reset_seconds=600,
    ).seq


def test_threads_are_private_to_their_owner(threads: SqlAlchemyGroupThreadStore) -> None:
    thread = threads.create("Launch plan", user_id=OWNER)

    assert threads.get(thread.id, user_id=OWNER) == thread
    assert threads.get(thread.id, user_id="mallory") is None
    assert threads.list_owned(user_id="mallory") == []
    assert thread.key == f"grp_{thread.id}"


def test_posts_number_in_order_and_page_from_the_newest(
    threads: SqlAlchemyGroupThreadStore,
) -> None:
    thread = threads.create("t", user_id=OWNER)
    seqs = [_post(threads, thread.id, "owner") for _ in range(5)]

    assert seqs == [1, 2, 3, 4, 5]
    assert [p.seq for p in threads.posts(thread.id, after_seq=1, limit=2)] == [4, 5]
    assert threads.get(thread.id, user_id=OWNER).last_seq == 5  # type: ignore[union-attr]


def test_agent_posts_past_the_cap_are_refused_until_the_owner_posts(
    threads: SqlAlchemyGroupThreadStore,
) -> None:
    thread = threads.create("t", user_id=OWNER)
    _post(threads, thread.id, "role")
    _post(threads, thread.id, "role")

    with pytest.raises(GroupThreadLoopGuardError, match="ask the owner"):
        _post(threads, thread.id, "role")
    assert threads.get(thread.id, user_id=OWNER).last_seq == 2  # type: ignore[union-attr]

    _post(threads, thread.id, "owner")
    assert _post(threads, thread.id, "session") == 4


def test_a_removed_member_can_rejoin_from_a_new_position(
    threads: SqlAlchemyGroupThreadStore,
) -> None:
    thread = threads.create("t", user_id=OWNER)
    member = threads.add_member(thread.id, "role", "omnigent/Product", delivered_seq=0)
    with pytest.raises(OmnigentError, match="already a member"):
        threads.add_member(thread.id, "role", "omnigent/Product", delivered_seq=0)

    assert threads.remove_member(thread.id, member.id)
    assert not threads.remove_member(thread.id, member.id)
    assert threads.members(thread.id) == []
    assert threads.threads_with_member([("role", "omnigent/Product")], user_id=OWNER) == []

    again = threads.add_member(thread.id, "role", "omnigent/Product", delivered_seq=7)
    assert (again.id, again.delivered_seq, again.left_at) == (member.id, 7, None)
    assert threads.threads_with_member([("role", "omnigent/Product")], user_id=OWNER) == [
        threads.get(thread.id, user_id=OWNER)
    ]


def test_mark_delivered_records_position_and_recipient(
    threads: SqlAlchemyGroupThreadStore,
) -> None:
    thread = threads.create("t", user_id=OWNER)
    member = threads.add_member(thread.id, "session", "a" * 32, delivered_seq=0)
    threads.mark_delivered(member.id, 3, "b" * 32)

    (stored,) = threads.members(thread.id)
    assert (stored.delivered_seq, stored.delivered_to) == (3, "b" * 32)

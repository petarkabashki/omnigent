"""Unit tests for group thread addressing and catch-up."""

from __future__ import annotations

import pytest

from omnigent.entities.group_thread import GroupThreadMember, GroupThreadPost
from omnigent.errors import OmnigentError
from omnigent.server.group_threads import (
    CATCH_UP_POSTS,
    normalize_member_ref,
    parse_thread_key,
    select_recipients,
    split_missed,
)

SESSION_ID = "a" * 32


def _member(member_id: str, kind: str, ref: str) -> GroupThreadMember:
    return GroupThreadMember(member_id, "t", kind, ref, 0, None, 0, None)  # type: ignore[arg-type]


def _post(seq: int) -> GroupThreadPost:
    return GroupThreadPost(f"p{seq}", "t", seq, "owner", "owner", f"post {seq}", 0)


MEMBERS = [_member("1", "role", "omnigent/Product"), _member("2", "session", SESSION_ID)]


def test_owner_default_reaches_everyone_and_agent_default_reaches_nobody() -> None:
    assert select_recipients(MEMBERS, None, by_owner=True, author=None) == MEMBERS
    assert select_recipients(MEMBERS, None, by_owner=False, author=MEMBERS[1]) == []
    assert select_recipients(MEMBERS, "everyone", by_owner=False, author=MEMBERS[1]) == [
        MEMBERS[0]
    ]


def test_named_recipients_match_roles_without_case_and_reject_strangers() -> None:
    assert select_recipients(MEMBERS, ["OMNIGENT/product"], by_owner=False, author=None) == [
        MEMBERS[0]
    ]
    with pytest.raises(OmnigentError, match="Not members of this thread: x/Y"):
        select_recipients(MEMBERS, ["x/Y"], by_owner=True, author=None)


def test_refs_and_keys_are_normalized_or_refused() -> None:
    assert normalize_member_ref("role", " /Architect ") == "/Architect"
    assert normalize_member_ref("session", SESSION_ID.upper()) == SESSION_ID
    assert parse_thread_key(f"grp_{SESSION_ID}") == SESSION_ID
    for kind, ref in (("role", "Architect"), ("session", "nope")):
        with pytest.raises(OmnigentError):
            normalize_member_ref(kind, ref)  # type: ignore[arg-type]
    with pytest.raises(OmnigentError, match="not found"):
        parse_thread_key("grp_nope")


def test_catch_up_keeps_the_newest_missed_posts_and_counts_the_rest() -> None:
    posts = [_post(seq) for seq in range(1, 31)]

    missed, omitted = split_missed(posts, delivered_seq=5, upto_seq=30, current_seq=30)

    assert [p.seq for p in missed] == list(range(30 - CATCH_UP_POSTS, 30))
    assert omitted == 24 - CATCH_UP_POSTS
    assert split_missed(posts[:3], delivered_seq=3, upto_seq=3, current_seq=None) == ([], 0)

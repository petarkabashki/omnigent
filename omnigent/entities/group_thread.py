"""Group thread entities — persisted in the ``group_threads`` tables.

A :class:`GroupThread` is one conversation between the owner and several
members. A member is a role (addressed by handle, resolved to its holder at
delivery) or a session. :class:`GroupThreadPost` rows are the canonical
transcript, numbered by ``seq`` within the thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MemberKind = Literal["role", "session"]
AuthorKind = Literal["owner", "role", "session"]
GROUP_THREAD_PREFIX = "grp_"


def group_thread_key(thread_id: str) -> str:
    """The external key of a group thread, e.g. ``grp_<32 hex>``."""
    return f"{GROUP_THREAD_PREFIX}{thread_id}"


@dataclass
class GroupThread:
    """
    A group thread.

    :param id: UUID primary key (bare 32-char hex).
    :param user_id: Owner, or ``None`` in single-user mode.
    :param title: Short label shown to the owner and members.
    :param summary: Current state of the discussion, used in late-joiner primers.
    :param state: ``open`` or ``done``.
    :param hops: Agent posts since the owner last posted, for the loop guard.
    :param hops_at: Unix epoch seconds of the last counted agent post.
    :param last_seq: ``seq`` of the newest post, ``0`` when empty.
    """

    id: str
    user_id: str | None
    title: str
    summary: str
    state: str
    hops: int
    hops_at: int | None
    last_seq: int
    created_at: int
    updated_at: int | None

    @property
    def key(self) -> str:
        """External key, ``grp_<id>``."""
        return group_thread_key(self.id)


@dataclass
class GroupThreadMember:
    """
    One member of a group thread.

    :param kind: ``role`` (``ref`` is a ``project/Role`` handle) or ``session`` (``ref`` is a
        session id).
    :param delivered_seq: Newest post seq this member has seen, by delivery or primer.
    :param delivered_to: Session that got the last delivery; a different holder of the
        same role gets a primer before new posts.
    :param left_at: Set when the owner removed the member; it then gets no posts.
    """

    id: str
    thread_id: str
    kind: MemberKind
    ref: str
    joined_at: int
    left_at: int | None
    delivered_seq: int
    delivered_to: str | None


@dataclass
class GroupThreadPost:
    """
    One post in a group thread's transcript.

    :param author_kind: ``owner``, ``role`` (``author_ref`` is the handle) or ``session``.
    :param mentions: Member refs the post named; they get a turn.
    """

    id: str
    thread_id: str
    seq: int
    author_kind: AuthorKind
    author_ref: str
    text: str
    created_at: int
    mentions: list[str] = field(default_factory=list)

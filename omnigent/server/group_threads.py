"""Group threads: who a post reaches and what each member is shown.

The transcript lives in the group thread store. Delivery is a ``message`` event
that starts one turn: a role member gets it in its holder's side chat for the
thread, a session member in that session. A member that was not addressed gets
nothing until it is; its next delivery then carries the posts it missed.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnigent.entities.group_thread import (
    GROUP_THREAD_PREFIX,
    GroupThread,
    GroupThreadMember,
    GroupThreadPost,
    MemberKind,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.role_threads import OWNER_SENDER
from omnigent.server.session_aliases import validate_alias
from omnigent.util.uuid import uuid_to_bytes

CATCH_UP_POSTS = 10  # missed posts bundled into one delivery


def _uuid_hex(value: str) -> str | None:
    try:
        return uuid_to_bytes(value.strip()).hex()
    except ValueError:
        return None


def parse_thread_key(value: str) -> str:
    """The thread id from ``grp_<hex>`` or a bare id."""
    thread_id = _uuid_hex(value.strip().removeprefix(GROUP_THREAD_PREFIX))
    if thread_id is None:
        raise OmnigentError("Group thread not found", code=ErrorCode.NOT_FOUND)
    return thread_id


def normalize_member_ref(kind: MemberKind, ref: str) -> str:
    """A ``project/Role`` handle (``/Role`` when unfiled) or a session id."""
    text = ref.strip()
    if kind == "session":
        session_id = _uuid_hex(text)
        if session_id is None:
            raise OmnigentError(f"{ref!r} is not a session id", code=ErrorCode.INVALID_INPUT)
        return session_id
    path, slash, name = text.rpartition("/")
    if not slash or any(char in text for char in "[]\n\r"):
        raise OmnigentError(
            f"{ref!r} is not a role handle; use 'project/Role'", code=ErrorCode.INVALID_INPUT
        )
    return f"{path}/{validate_alias(name)}"


def same_ref(member: GroupThreadMember, ref: str) -> bool:
    """Role handles compare without case, as role names do."""
    if member.kind == "role":
        return member.ref.casefold() == ref.strip().casefold()
    return member.ref == (_uuid_hex(ref) or ref)


def select_recipients(
    members: Sequence[GroupThreadMember],
    to: str | list[str] | None,
    *,
    by_owner: bool,
    author: GroupThreadMember | None,
) -> list[GroupThreadMember]:
    """The members a post gives a turn; the author never addresses itself.

    ``None`` means everyone for the owner and nobody for an agent, so agents
    cannot start a round of turns without naming who should answer.
    """
    if to is None:
        to = "everyone" if by_owner else []
    if to == "everyone":
        chosen = list(members)
    else:
        unknown = [ref for ref in to if not any(same_ref(m, ref) for m in members)]
        if unknown:
            raise OmnigentError(
                f"Not members of this thread: {', '.join(unknown)}", code=ErrorCode.INVALID_INPUT
            )
        chosen = [m for m in members if any(same_ref(m, ref) for ref in to)]
    return [m for m in chosen if author is None or m.id != author.id]


def author_name(post: GroupThreadPost) -> str:
    """The sender shown in a post's header."""
    if post.author_kind == "owner":
        return OWNER_SENDER
    if post.author_kind == "session":
        return f"session:{post.author_ref}"
    return post.author_ref


def _post_line(post: GroupThreadPost) -> str:
    return f"#{post.seq} {author_name(post)}: {post.text}"


def compose_delivery(
    thread: GroupThread,
    members: Sequence[GroupThreadMember],
    *,
    message_id: str,
    post: GroupThreadPost | None,
    missed: Sequence[GroupThreadPost],
    omitted: int,
    primer: bool,
) -> str:
    """The text of one delivery: header, thread context, missed posts, then the post.

    Without ``post`` this is the primer an invited member gets on joining.
    """
    sender = author_name(post) if post is not None else OWNER_SENDER
    lines = [
        f"[Message from {sender} (message {message_id})] [thread {thread.key}]",
        f'Group thread "{thread.title}" with the owner and '
        + ", ".join(m.ref if m.kind == "role" else f"session:{m.ref}" for m in members)
        + ".",
        f'Reply with sys_thread_post(thread="{thread.key}", message=…, to=[…]); '
        "only the members you name in `to` get a turn.",
    ]
    if post is None:
        lines.append("You were added to this thread.")
    if primer:
        lines.append(f"Summary so far: {thread.summary or '(none yet)'}")
    if missed or omitted:
        lines.append("")
        lines.append("Posts you have not seen:")
        if omitted:
            lines.append(
                f"({omitted} older posts not shown; read them with "
                f'sys_thread_history(counterpart="{thread.key}").)'
            )
        lines.extend(_post_line(p) for p in missed)
    if post is not None:
        lines.append("")
        lines.append(f"Post #{post.seq} from {sender}:")
        lines.append(post.text)
    return "\n".join(lines)


def split_missed(
    posts: Sequence[GroupThreadPost], *, delivered_seq: int, upto_seq: int, current_seq: int | None
) -> tuple[list[GroupThreadPost], int]:
    """Missed posts after ``delivered_seq`` (excluding the current one) and how many were cut."""
    missed = [p for p in posts if delivered_seq < p.seq <= upto_seq and p.seq != current_seq]
    missed = missed[-CATCH_UP_POSTS:]
    total = upto_seq - delivered_seq - (1 if current_seq is not None else 0)
    return missed, max(total - len(missed), 0)

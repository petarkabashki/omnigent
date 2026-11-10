"""SQLAlchemy-backed group thread store, on the projects database."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlGroupThread,
    SqlGroupThreadMember,
    SqlGroupThreadPost,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.group_thread import (
    AuthorKind,
    GroupThread,
    GroupThreadMember,
    GroupThreadPost,
    MemberKind,
)
from omnigent.errors import ErrorCode, OmnigentError

_STATES = {"open": 1, "done": 2}
_MEMBER_KINDS: dict[MemberKind, int] = {"role": 1, "session": 2}
_AUTHOR_KINDS: dict[AuthorKind, int] = {"owner": 1, "role": 2, "session": 3}


def _decode(codes: dict, value: int) -> str:
    return next(name for name, code in codes.items() if code == value)


class GroupThreadLoopGuardError(OmnigentError):
    """An agent post past the hop cap; the owner must post before agents go on."""


def _thread(row: SqlGroupThread) -> GroupThread:
    return GroupThread(
        id=row.id,
        user_id=row.user_id,
        title=row.title,
        summary=row.summary or "",
        state=_decode(_STATES, row.state),
        hops=row.hops,
        hops_at=row.hops_at,
        last_seq=row.last_seq,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _member(row: SqlGroupThreadMember) -> GroupThreadMember:
    return GroupThreadMember(
        id=row.id,
        thread_id=row.thread_id,
        kind=_decode(_MEMBER_KINDS, row.kind),  # type: ignore[arg-type]
        ref=row.ref,
        joined_at=row.joined_at,
        left_at=row.left_at,
        delivered_seq=row.delivered_seq,
        delivered_to=row.delivered_to,
    )


def _post(row: SqlGroupThreadPost) -> GroupThreadPost:
    return GroupThreadPost(
        id=row.id,
        thread_id=row.thread_id,
        seq=row.seq,
        author_kind=_decode(_AUTHOR_KINDS, row.author_kind),  # type: ignore[arg-type]
        author_ref=row.author_ref,
        text=row.text,
        created_at=row.created_at,
        mentions=json.loads(row.mentions) if row.mentions else [],
    )


class SqlAlchemyGroupThreadStore:
    """Persists group threads; every read is scoped to the owner."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the store.

        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.group_thread_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.group_thread_store", immediate=True
        )

    def _owned(self, user_id: str | None) -> Select[tuple[SqlGroupThread]]:
        return select(SqlGroupThread).where(
            SqlGroupThread.workspace_id == current_workspace_id(),
            SqlGroupThread.owner_key == (user_id or ""),
        )

    def _members_query(self, thread_id: str) -> Select[tuple[SqlGroupThreadMember]]:
        return select(SqlGroupThreadMember).where(
            SqlGroupThreadMember.workspace_id == current_workspace_id(),
            SqlGroupThreadMember.thread_id == thread_id,
        )

    def create(self, title: str, *, user_id: str | None) -> GroupThread:
        """Insert an empty open thread owned by ``user_id``."""
        now = now_epoch()
        thread_id = uuid.uuid4().hex

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            session.add(
                SqlGroupThread(
                    id=thread_id,
                    user_id=user_id,
                    owner_key=user_id or "",
                    title=title,
                    summary="",
                    state=_STATES["open"],
                    hops=0,
                    last_seq=0,
                    created_at=now,
                )
            )

        run_write_transaction(self._session_immediate, "create_group_thread", write)
        created = self.get(thread_id, user_id=user_id)
        assert created is not None
        return created

    def get(self, thread_id: str, *, user_id: str | None) -> GroupThread | None:
        """Return the owner's thread, else ``None``."""
        with self._session("select_group_thread") as session:
            row = session.scalar(self._owned(user_id).where(SqlGroupThread.id == thread_id))
            return _thread(row) if row is not None else None

    def list_owned(self, *, user_id: str | None) -> list[GroupThread]:
        """Every thread of the owner, newest first."""
        with self._session("list_group_threads") as session:
            rows = session.scalars(self._owned(user_id).order_by(SqlGroupThread.created_at.desc()))
            return [_thread(row) for row in rows]

    def threads_with_member(
        self, refs: Iterable[tuple[MemberKind, str]], *, user_id: str | None
    ) -> list[GroupThread]:
        """The owner's threads in which any of ``refs`` is a current member."""
        wanted = [
            and_(SqlGroupThreadMember.kind == _MEMBER_KINDS[kind], SqlGroupThreadMember.ref == ref)
            for kind, ref in refs
        ]
        if not wanted:
            return []
        with self._session("list_group_threads_of_member") as session:
            thread_ids = select(SqlGroupThreadMember.thread_id).where(
                SqlGroupThreadMember.workspace_id == current_workspace_id(),
                SqlGroupThreadMember.left_at.is_(None),
                or_(*wanted),
            )
            rows = session.scalars(
                self._owned(user_id)
                .where(SqlGroupThread.id.in_(thread_ids))
                .order_by(SqlGroupThread.created_at.desc())
            )
            return [_thread(row) for row in rows]

    def members(self, thread_id: str, *, include_left: bool = False) -> list[GroupThreadMember]:
        """The thread's members in join order."""
        query = self._members_query(thread_id)
        if not include_left:
            query = query.where(SqlGroupThreadMember.left_at.is_(None))
        with self._session("list_group_thread_members") as session:
            rows = session.scalars(query.order_by(SqlGroupThreadMember.joined_at))
            return [_member(row) for row in rows]

    def add_member(
        self, thread_id: str, kind: MemberKind, ref: str, *, delivered_seq: int
    ) -> GroupThreadMember:
        """Add a member, or bring back one that left, as having seen ``delivered_seq``."""
        now = now_epoch()

        def write(session: Session) -> str:
            row = session.scalar(
                self._members_query(thread_id).where(
                    SqlGroupThreadMember.kind == _MEMBER_KINDS[kind],
                    SqlGroupThreadMember.ref == ref,
                )
            )
            if row is not None:
                if row.left_at is None:
                    raise OmnigentError(f"{ref} is already a member", code=ErrorCode.CONFLICT)
                row.left_at = None
                row.joined_at = now
                row.delivered_seq = delivered_seq
                row.delivered_to = None
                return row.id
            member_id = uuid.uuid4().hex
            session.add(
                SqlGroupThreadMember(
                    id=member_id,
                    thread_id=thread_id,
                    kind=_MEMBER_KINDS[kind],
                    ref=ref,
                    joined_at=now,
                    delivered_seq=delivered_seq,
                )
            )
            return member_id

        member_id = run_write_transaction(
            self._session_immediate, "add_group_thread_member", write
        )
        return next(m for m in self.members(thread_id) if m.id == member_id)

    def remove_member(self, thread_id: str, member_id: str) -> bool:
        """Mark a member as left; returns ``False`` when it was not a current member."""

        def write(session: Session) -> bool:
            result = session.execute(
                update(SqlGroupThreadMember)
                .where(
                    SqlGroupThreadMember.workspace_id == current_workspace_id(),
                    SqlGroupThreadMember.thread_id == thread_id,
                    SqlGroupThreadMember.id == member_id,
                    SqlGroupThreadMember.left_at.is_(None),
                )
                .values(left_at=now_epoch())
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

        return run_write_transaction(self._session_immediate, "remove_group_thread_member", write)

    def mark_delivered(self, member_id: str, seq: int, delivered_to: str | None) -> None:
        """Record that ``delivered_to`` has seen the thread up to ``seq``."""

        def write(session: Session) -> None:
            session.execute(
                update(SqlGroupThreadMember)
                .where(
                    SqlGroupThreadMember.workspace_id == current_workspace_id(),
                    SqlGroupThreadMember.id == member_id,
                )
                .values(delivered_seq=seq, delivered_to=delivered_to)
            )

        run_write_transaction(self._session_immediate, "mark_group_thread_delivered", write)

    def append_post(
        self,
        thread_id: str,
        *,
        user_id: str | None,
        author_kind: AuthorKind,
        author_ref: str,
        text: str,
        mentions: list[str],
        max_hops: int,
        hop_reset_seconds: int,
    ) -> GroupThreadPost:
        """Append a post as ``last_seq + 1``; an owner post resets the loop guard.

        :raises GroupThreadLoopGuardError: an agent post past ``max_hops`` agent posts
            without an owner post; nothing is written.
        """
        now = now_epoch()
        post_id = uuid.uuid4().hex

        def write(session: Session) -> int:
            row = session.scalar(self._owned(user_id).where(SqlGroupThread.id == thread_id))
            if row is None:
                raise OmnigentError("Group thread not found", code=ErrorCode.NOT_FOUND)
            if author_kind == "owner":
                hops = 0
            else:
                fresh = row.hops_at is not None and now - row.hops_at <= hop_reset_seconds
                hops = (row.hops if fresh else 0) + 1
                if hops > max_hops:
                    raise GroupThreadLoopGuardError(
                        f"Thread loop guard: {max_hops} agent posts in this group without "
                        "the owner posting. Stop posting and ask the owner.",
                        code=ErrorCode.CONFLICT,
                    )
            seq = row.last_seq + 1
            row.last_seq = seq
            row.hops = hops
            row.hops_at = None if author_kind == "owner" else now
            row.updated_at = now
            session.add(
                SqlGroupThreadPost(
                    id=post_id,
                    thread_id=thread_id,
                    seq=seq,
                    author_kind=_AUTHOR_KINDS[author_kind],
                    author_ref=author_ref,
                    text=text,
                    mentions=json.dumps(mentions) if mentions else None,
                    created_at=now,
                )
            )
            return seq

        seq = run_write_transaction(self._session_immediate, "append_group_thread_post", write)
        return GroupThreadPost(
            id=post_id,
            thread_id=thread_id,
            seq=seq,
            author_kind=author_kind,
            author_ref=author_ref,
            text=text,
            created_at=now,
            mentions=list(mentions),
        )

    def posts(
        self, thread_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[GroupThreadPost]:
        """The newest ``limit`` posts after ``after_seq``, oldest first."""
        with self._session("list_group_thread_posts") as session:
            rows = session.scalars(
                select(SqlGroupThreadPost)
                .where(
                    SqlGroupThreadPost.workspace_id == current_workspace_id(),
                    SqlGroupThreadPost.thread_id == thread_id,
                    SqlGroupThreadPost.seq > after_seq,
                )
                .order_by(SqlGroupThreadPost.seq.desc())
                .limit(limit)
            )
            return [_post(row) for row in reversed(list(rows))]

    def update_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None,
        summary: str | None = None,
        state: str | None = None,
    ) -> GroupThread:
        """Set the thread's summary and/or state."""
        values: dict[str, object] = {"updated_at": now_epoch()}
        if summary is not None:
            values["summary"] = summary
        if state is not None:
            values["state"] = _STATES[state]

        def write(session: Session) -> None:
            session.execute(
                update(SqlGroupThread)
                .where(
                    SqlGroupThread.workspace_id == current_workspace_id(),
                    SqlGroupThread.owner_key == (user_id or ""),
                    SqlGroupThread.id == thread_id,
                )
                .values(**values)
            )

        run_write_transaction(self._session_immediate, "update_group_thread", write)
        updated = self.get(thread_id, user_id=user_id)
        if updated is None:
            raise OmnigentError("Group thread not found", code=ErrorCode.NOT_FOUND)
        return updated

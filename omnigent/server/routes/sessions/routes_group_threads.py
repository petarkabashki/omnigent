"""Group thread routes: the owner and member agents talking in one thread.

Registered before the ``/sessions/{session_id}`` routes so ``group-threads`` is
not read as a session id. Delivery reuses the fork handler (a role member's
side chat) and the event dispatcher, which exist only once the core and event
routes are registered, so they arrive through :class:`GroupThreadHandlers`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.entities import Conversation
from omnigent.entities.group_thread import GroupThread, GroupThreadMember, GroupThreadPost
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import AuthProvider
from omnigent.server.group_threads import (
    compose_delivery,
    normalize_member_ref,
    parse_thread_key,
    same_ref,
    select_recipients,
    split_missed,
)
from omnigent.server.role_threads import (
    HOP_RESET_SECONDS,
    MAX_HOPS,
    THREAD_OF_LABEL,
    find_thread_side_chat,
    record_inbound,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    GroupThreadCreateRequest,
    GroupThreadInviteRequest,
    GroupThreadPostRequest,
    GroupThreadUpdateRequest,
    SessionEventInput,
    SessionForkRequest,
    SessionResponse,
)
from omnigent.server.session_aliases import resolve_session_address, session_handle
from omnigent.server.session_orchestration import (
    require_orchestration_source,
    require_owned_session,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.group_thread_store import SqlAlchemyGroupThreadStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore

ForkHandler = Callable[[Request, str, SessionForkRequest], Awaitable[SessionResponse]]
EventHandler = Callable[[Request, str, SessionEventInput], Awaitable[dict[str, bool | str]]]


@dataclasses.dataclass
class GroupThreadHandlers:
    """Handlers bound after the core and event routes are registered."""

    fork: ForkHandler | None = None
    post_event: EventHandler | None = None


@dataclasses.dataclass
class _Caller:
    """Who is calling: the owner (``source`` is ``None``) or a member agent's session."""

    user_id: str | None
    source: Conversation | None = None
    holder: Conversation | None = None  # the role holder a thread side chat speaks for

    @property
    def is_owner(self) -> bool:
        return self.source is None


def _thread_json(thread: GroupThread) -> dict[str, Any]:
    return {**dataclasses.asdict(thread), "key": thread.key}


def _post_json(post: GroupThreadPost) -> dict[str, Any]:
    return dataclasses.asdict(post)


def register_group_thread_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    project_store: ProjectStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    handlers: GroupThreadHandlers,
) -> None:
    """Register ``/sessions/group-threads`` routes on ``router``."""

    def _store() -> SqlAlchemyGroupThreadStore:
        store = getattr(project_store, "group_threads", None)
        if store is None:
            raise OmnigentError("Group threads are unavailable", code=ErrorCode.NOT_FOUND)
        return store

    def _caller(request: Request) -> _Caller:
        user_id = require_user(request, auth_provider)
        if not request.headers.get(ORCHESTRATION_SOURCE_HEADER):
            return _Caller(user_id)
        source = require_orchestration_source(
            request, user_id, conversation_store, permission_store
        )
        holder = source
        if source.labels.get(THREAD_OF_LABEL):
            holder_id = source.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            holder = (conversation_store.get_conversation(holder_id) if holder_id else None) or (
                source
            )
        return _Caller(user_id, source, holder)

    def _require_owner(caller: _Caller) -> None:
        if not caller.is_owner:
            raise OmnigentError(
                "Only the owner can do this in a group thread", code=ErrorCode.FORBIDDEN
            )

    def _caller_refs(caller: _Caller) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        if caller.holder is not None:
            handle = session_handle(caller.holder, project_store, caller.user_id)
            if handle:
                refs.append(("role", handle))
            refs.append(("session", caller.holder.id))
        if caller.source is not None and caller.source.id not in {r for _, r in refs}:
            refs.append(("session", caller.source.id))
        return refs

    def _caller_member(
        caller: _Caller, members: list[GroupThreadMember]
    ) -> GroupThreadMember | None:
        for kind, ref in _caller_refs(caller):
            for member in members:
                if member.kind == kind and same_ref(member, ref):
                    return member
        return None

    def _load(
        caller: _Caller, thread_key: str
    ) -> tuple[GroupThread, list[GroupThreadMember], GroupThreadMember | None]:
        thread = _store().get(parse_thread_key(thread_key), user_id=caller.user_id)
        if thread is None:
            raise OmnigentError("Group thread not found", code=ErrorCode.NOT_FOUND)
        members = _store().members(thread.id)
        author = None if caller.is_owner else _caller_member(caller, members)
        if not caller.is_owner and author is None:
            raise OmnigentError(
                "This session is not a member of that group thread", code=ErrorCode.FORBIDDEN
            )
        return thread, members, author

    def _owned_session(session_id: str, user_id: str | None) -> Conversation | None:
        try:
            if permission_store is not None and user_id:
                return require_owned_session(
                    session_id, user_id, conversation_store, permission_store
                )
        except OmnigentError:
            return None
        return conversation_store.get_conversation(session_id)

    def _member_session(member: GroupThreadMember, user_id: str | None) -> Conversation | None:
        """The session a member is reached at now; ``None`` for a vacant role or gone session."""
        if member.kind == "session":
            return _owned_session(member.ref, user_id)
        try:
            holder_id = resolve_session_address(
                conversation_store, project_store, address=member.ref, user_id=user_id, source=None
            )
        except OmnigentError:
            return None
        return _owned_session(holder_id, user_id)

    def _member_json(member: GroupThreadMember, user_id: str | None) -> dict[str, Any]:
        session = _member_session(member, user_id)
        return {
            **dataclasses.asdict(member),
            "state": "vacant" if session is None else "held",
            "session_id": session.id if session is not None else None,
        }

    async def _deliver(
        request: Request,
        user_id: str | None,
        thread: GroupThread,
        members: list[GroupThreadMember],
        member: GroupThreadMember,
        *,
        post: GroupThreadPost | None,
        primer: bool,
    ) -> str | None:
        """Give one member a turn; returns the session reached, ``None`` when unreachable."""
        assert handlers.fork is not None and handlers.post_event is not None
        reached = await asyncio.to_thread(_member_session, member, user_id)
        if reached is None:
            return None  # delivered_seq stays, so the posts wait for the next holder
        if member.kind == "role" and member.delivered_to and member.delivered_to != reached.id:
            primer = True  # the role changed hands since its last delivery
        upto = post.seq if post is not None else thread.last_seq
        posts = await asyncio.to_thread(
            _store().posts, thread.id, after_seq=member.delivered_seq, limit=12
        )
        missed, omitted = split_missed(
            posts,
            delivered_seq=member.delivered_seq,
            upto_seq=upto,
            current_seq=post.seq if post is not None else None,
        )
        message_id = f"msg_{secrets.token_hex(16)}"
        text = compose_delivery(
            thread,
            members,
            message_id=message_id,
            post=post,
            missed=missed,
            omitted=omitted,
            primer=primer,
        )
        target_id = reached.id
        if member.kind == "role":
            side = await asyncio.to_thread(
                find_thread_side_chat, conversation_store, reached.id, thread.key, user_id=user_id
            )
            if side is None:
                opened = await handlers.fork(
                    request, reached.id, SessionForkRequest(side_chat=True, thread=thread.key)
                )
                target_id = opened.id
            else:
                target_id = side.id
            await asyncio.to_thread(
                record_inbound,
                conversation_store,
                reached,
                thread.key,
                member.ref,
                message_id,
                post.text if post is not None else f"Added to {thread.title}",
                0,
            )
        data = {"role": "user", "content": [{"type": "input_text", "text": text}]}
        await handlers.post_event(request, target_id, SessionEventInput(type="message", data=data))
        await asyncio.to_thread(_store().mark_delivered, member.id, upto, reached.id)
        return target_id

    async def _post(
        request: Request,
        caller: _Caller,
        thread: GroupThread,
        members: list[GroupThreadMember],
        author: GroupThreadMember | None,
        body: GroupThreadPostRequest,
    ) -> dict[str, Any]:
        recipients = select_recipients(members, body.to, by_owner=caller.is_owner, author=author)
        if author is None:
            author_kind, author_ref = "owner", "owner"
        else:
            author_kind, author_ref = author.kind, author.ref
        post = await asyncio.to_thread(
            _store().append_post,
            thread.id,
            user_id=caller.user_id,
            author_kind=author_kind,
            author_ref=author_ref,
            text=body.message,
            mentions=[m.ref for m in recipients],
            max_hops=MAX_HOPS,
            hop_reset_seconds=HOP_RESET_SECONDS,
        )
        if body.summary is not None or (caller.is_owner and thread.state == "done"):
            thread = await asyncio.to_thread(
                _store().update_thread,
                thread.id,
                user_id=caller.user_id,
                summary=body.summary,
                state="open" if caller.is_owner else None,
            )
        thread = dataclasses.replace(thread, last_seq=post.seq)
        if author is not None:
            reached = caller.holder.id if caller.holder is not None else author.delivered_to
            await asyncio.to_thread(_store().mark_delivered, author.id, post.seq, reached)
        delivered: list[dict[str, str | None]] = []
        for member in recipients:
            target = await _deliver(
                request, caller.user_id, thread, members, member, post=post, primer=False
            )
            delivered.append({"member_id": member.id, "ref": member.ref, "session_id": target})
        return {"post": _post_json(post), "delivered": delivered}

    @router.post("/sessions/group-threads", status_code=201)
    async def create_group_thread(
        request: Request, body: GroupThreadCreateRequest
    ) -> dict[str, Any]:
        """The owner starts a group thread with its members and an optional first post."""
        caller = await asyncio.to_thread(_caller, request)
        _require_owner(caller)
        refs = [(m.kind, normalize_member_ref(m.kind, m.ref)) for m in body.members]
        thread = await asyncio.to_thread(_store().create, body.title, user_id=caller.user_id)
        for kind, ref in refs:
            await asyncio.to_thread(_store().add_member, thread.id, kind, ref, delivered_seq=0)
        result: dict[str, Any] = {"thread": _thread_json(thread)}
        if body.message:
            members = await asyncio.to_thread(_store().members, thread.id)
            result.update(
                await _post(
                    request,
                    caller,
                    thread,
                    members,
                    None,
                    GroupThreadPostRequest(message=body.message),
                )
            )
        return result

    @router.get("/sessions/group-threads")
    async def list_group_threads(request: Request) -> dict[str, Any]:
        """The owner's threads, or the threads the calling agent is a member of."""

        def _list() -> list[GroupThread]:
            caller = _caller(request)
            if caller.is_owner:
                return _store().list_owned(user_id=caller.user_id)
            return _store().threads_with_member(
                _caller_refs(caller),  # type: ignore[arg-type]
                user_id=caller.user_id,
            )

        threads = await asyncio.to_thread(_list)
        return {"threads": [_thread_json(t) for t in threads]}

    @router.get("/sessions/group-threads/{thread_key}")
    async def get_group_thread(request: Request, thread_key: str) -> dict[str, Any]:
        """A thread with its members and where each is reached now."""

        def _get() -> dict[str, Any]:
            caller = _caller(request)
            thread, members, _ = _load(caller, thread_key)
            return {
                "thread": _thread_json(thread),
                "members": [_member_json(m, caller.user_id) for m in members],
            }

        return await asyncio.to_thread(_get)

    @router.patch("/sessions/group-threads/{thread_key}")
    async def update_group_thread(
        request: Request, thread_key: str, body: GroupThreadUpdateRequest
    ) -> dict[str, Any]:
        """The owner renames, summarizes or closes a thread."""

        def _update() -> GroupThread:
            caller = _caller(request)
            _require_owner(caller)
            thread, _, _ = _load(caller, thread_key)
            return _store().update_thread(
                thread.id,
                user_id=caller.user_id,
                title=body.title,
                summary=body.summary,
                state=body.state,
            )

        return {"thread": _thread_json(await asyncio.to_thread(_update))}

    @router.get("/sessions/group-threads/{thread_key}/posts")
    async def list_group_thread_posts(
        request: Request,
        thread_key: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """The newest ``limit`` posts after seq ``after``, oldest first."""

        def _posts() -> dict[str, Any]:
            caller = _caller(request)
            thread, _, _ = _load(caller, thread_key)
            posts = _store().posts(thread.id, after_seq=after, limit=limit)
            return {"thread": _thread_json(thread), "posts": [_post_json(p) for p in posts]}

        return await asyncio.to_thread(_posts)

    @router.post("/sessions/group-threads/{thread_key}/members", status_code=201)
    async def invite_group_thread_member(
        request: Request, thread_key: str, body: GroupThreadInviteRequest
    ) -> dict[str, Any]:
        """The owner invites a role or session; a primer is its first event."""
        caller = await asyncio.to_thread(_caller, request)
        _require_owner(caller)
        thread, _, _ = await asyncio.to_thread(_load, caller, thread_key)
        ref = normalize_member_ref(body.kind, body.ref)
        if (
            body.kind == "session"
            and await asyncio.to_thread(_owned_session, ref, caller.user_id) is None
        ):
            raise OmnigentError(f"Session {ref} not found", code=ErrorCode.NOT_FOUND)
        start = 0 if body.primer == "full" else thread.last_seq
        member = await asyncio.to_thread(
            _store().add_member, thread.id, body.kind, ref, delivered_seq=start
        )
        members = await asyncio.to_thread(_store().members, thread.id)
        session_id = None
        if body.primer != "none" and thread.last_seq > 0:
            session_id = await _deliver(
                request,
                caller.user_id,
                thread,
                members,
                member,
                post=None,
                primer=body.primer == "summary",
            )
        return {"member": _member_json(member, caller.user_id), "primed_session_id": session_id}

    @router.delete("/sessions/group-threads/{thread_key}/members/{member_id}")
    async def remove_group_thread_member(
        request: Request, thread_key: str, member_id: str
    ) -> dict[str, Any]:
        """The owner removes a member; its past posts stay in the transcript."""

        def _remove() -> bool:
            caller = _caller(request)
            _require_owner(caller)
            thread, _, _ = _load(caller, thread_key)
            return _store().remove_member(thread.id, member_id)

        if not await asyncio.to_thread(_remove):
            raise OmnigentError("Member not found", code=ErrorCode.NOT_FOUND)
        return {"removed": member_id}

    @router.post("/sessions/group-threads/{thread_key}/posts", status_code=201)
    async def post_to_group_thread(
        request: Request, thread_key: str, body: GroupThreadPostRequest
    ) -> dict[str, Any]:
        """The owner or a member agent posts; the members in ``to`` get a turn."""
        caller = await asyncio.to_thread(_caller, request)
        thread, members, author = await asyncio.to_thread(_load, caller, thread_key)
        if not caller.is_owner and thread.state == "done":
            raise OmnigentError(
                "This group thread is done; ask the owner to reopen it", code=ErrorCode.CONFLICT
            )
        return await _post(request, caller, thread, members, author, body)

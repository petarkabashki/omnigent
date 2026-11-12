"""Hub-backed view sources with the row shape of ``agentic_base.query``.

Every row carries the full agentic-base field set; a field the hub cannot fill is ``None``.
``last_event_ts`` is the hub's ``updated_at`` in milliseconds until the event graph backs
these sources. Reads go through the stores with the caller's own scoping.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.entities.conversation import Conversation
from omnigent.server.role_threads import thread_index
from omnigent.server.session_aliases import role_handle
from omnigent.stores.conversation_store import (
    FORK_OF_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.project_store import ProjectStore
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.views.spec import (
    MAX_ROWS,
    RolesSource,
    SessionsSource,
    SubagentsSource,
    ThreadsSource,
)

DEFAULT_ORIGIN = "hub"
# Upper bound on hub rows read for one resolve, before filtering.
MAX_SCANNED = 2000
DEFAULT_DEADLINE_SECONDS = 5.0
_PAGE_SIZE = 200
_DESCRIPTION_LABEL = "omnigent.claude_native.description"
_HUB_KIND = {"default": "session", "sub_agent": "subagent"}


class SourceError(ValueError):
    """A source request the hub cannot answer, e.g. an unknown session for ``subagents``."""


def ms(value: int | None) -> int | None:
    """Hub timestamps in milliseconds; seconds are scaled as agentic-base does."""
    if value is None:
        return None
    return value if value > 10**12 else value * 1000


def order_rows(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """``agentic_base.query._order``: nulls last, ties broken by id."""
    descending, key = order.startswith("-"), order.lstrip("-")
    present = [row for row in rows if row.get(key) is not None]
    missing = [row for row in rows if row.get(key) is None]
    present.sort(key=lambda row: (row[key], row["id"]), reverse=descending)
    return present + sorted(missing, key=lambda row: row["id"])


@dataclass
class Resolved:
    rows: list[dict[str, Any]]
    truncated: bool = False


@dataclass
class HubSources:
    """The four named sources for one user."""

    conversations: ConversationStore
    projects: ProjectStore | None
    user_id: str | None
    host_name: Callable[[str], str | None] = lambda _host_id: None
    origin: str = DEFAULT_ORIGIN
    deadline: float = field(default_factory=lambda: time.monotonic() + DEFAULT_DEADLINE_SECONDS)
    _project_paths: dict[str, str | None] = field(default_factory=dict)
    _hosts: dict[str, str | None] = field(default_factory=dict)
    scanned_all: bool = True

    def resolve(
        self, source: SessionsSource | ThreadsSource | RolesSource | SubagentsSource
    ) -> Resolved:
        if isinstance(source, SessionsSource):
            rows = self.sessions(**source.params.model_dump())
        elif isinstance(source, ThreadsSource):
            rows = self.threads(**source.params.model_dump())
        elif isinstance(source, RolesSource):
            rows = self.roles(**source.params.model_dump())
        else:
            rows = self.subagents(**source.params.model_dump())
        ordered = order_rows(rows, source.order)
        limit = min(source.limit, MAX_ROWS)
        return Resolved(ordered[:limit], len(ordered) > limit or not self.scanned_all)

    # ---- shared lookups -------------------------------------------------
    def _check_deadline(self) -> bool:
        if time.monotonic() > self.deadline:
            self.scanned_all = False
            return False
        return True

    def _hub_ref(self, kind: str, object_id: str) -> dict[str, str]:
        return {"origin": self.origin, "kind": kind, "id": object_id}

    def _project_path(self, project_id: str | None) -> str | None:
        if not project_id or self.projects is None:
            return None
        if project_id not in self._project_paths:
            self._project_paths[project_id] = self.projects.get_path(
                project_id, user_id=self.user_id
            )
        return self._project_paths[project_id]

    def _host_name(self, host_id: str | None) -> str | None:
        if host_id is None:
            return None
        if host_id not in self._hosts:
            self._hosts[host_id] = self.host_name(host_id)
        return self._hosts[host_id]

    def _owned_roles(self) -> list[Any]:
        if self.projects is None or getattr(self.projects, "roles", None) is None:
            return []
        return self.projects.roles.list_owned(user_id=self.user_id)

    def _role_handle(self, role: Any) -> str:
        return role_handle(self._project_path(role.project_id) or "", role.name)

    def _roles_by_session(self, session_ids: Iterable[str]) -> dict[str, list[str]]:
        held: dict[str, list[str]] = {}
        if self.projects is None or getattr(self.projects, "roles", None) is None:
            return held
        for role in self.projects.roles.held_by(list(session_ids)):
            if role.holder_session_id:
                held.setdefault(role.holder_session_id, []).append(self._role_handle(role))
        return {session_id: sorted(handles) for session_id, handles in held.items()}

    def _scan(self, **filters: Any) -> list[Conversation]:
        found: list[Conversation] = []
        after: str | None = None
        while len(found) < MAX_SCANNED and self._check_deadline():
            page = self.conversations.list_conversations(
                limit=_PAGE_SIZE,
                after=after,
                accessible_by=self.user_id,
                order="desc",
                sort_by="updated_at",
                **filters,
            )
            found.extend(page.data)
            if not page.has_more or not page.data:
                return found
            after = page.last_id
        self.scanned_all = False
        return found[:MAX_SCANNED]

    # ---- session rows ---------------------------------------------------
    def session_rows(self, conversations: list[Conversation]) -> list[dict[str, Any]]:
        roles = self._roles_by_session(conv.id for conv in conversations)
        return [self._session_row(conv, roles.get(conv.id, [])) for conv in conversations]

    def _session_row(self, conv: Conversation, roles: list[str]) -> dict[str, Any]:
        labels = conv.labels or {}
        parent = conv.parent_conversation_id
        spawn_kind = "subagent" if conv.kind == "sub_agent" else "child"
        fork_source = labels.get(FORK_OF_LABEL_KEY) or labels.get(FORK_SOURCE_LABEL_KEY)
        if fork_source:
            parent, spawn_kind = parent or fork_source, "fork"
        if conv.side_chat_parent_id:
            parent, spawn_kind = parent or conv.side_chat_parent_id, "side_chat"
        elif labels.get(SIDE_CHAT_LABEL_KEY) and fork_source:
            spawn_kind = "side_chat"
        closed = is_session_closed(labels, conv.title)
        stopped = conv.archived or closed
        return {
            "id": conv.id,
            "hub_ref": self._hub_ref("session", conv.id),
            "title": conv.sub_agent_name or conv.title or labels.get(_DESCRIPTION_LABEL),
            "kind": _HUB_KIND.get(conv.kind, conv.kind),
            "harness": labels.get(WRAPPER_LABEL_KEY) or None,
            "spawn_kind": spawn_kind if parent else None,
            "parent_actor_id": parent,
            "project_id": conv.project_id,
            "project": self._project_path(conv.project_id),
            "host_id": conv.host_id,
            "host": self._host_name(conv.host_id),
            "stopped": bool(stopped),
            "stop_reason": ("closed" if closed else "archived") if stopped else None,
            "roles": roles,
            "last_event_ts": ms(conv.updated_at),
        }

    # ---- named sources --------------------------------------------------
    def sessions(
        self,
        project: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        archived: bool | None = None,
        host: str | None = None,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        hub_kind = {"session": "default", "subagent": "sub_agent"}.get(kind or "")
        scanned = self._scan(
            kind=hub_kind,
            has_agent_id=True,
            # No hub-side title search: the row title falls back to the sub-agent name and
            # description label, which only the filter below sees; truncated covers the cap.
            include_archived=archived is not False,
        )
        rows = []
        for row in self.session_rows(scanned):
            if project is not None and project not in (row["project"], row["project_id"]):
                continue
            if kind is not None and row["kind"] != kind:
                continue
            if search is not None and search.lower() not in (row["title"] or "").lower():
                continue
            if archived is not None and row["stopped"] != archived:
                continue
            if host is not None and host not in (row["host"], row["host_id"]):
                continue
            if role is not None and role not in row["roles"]:
                continue
            rows.append(row)
        return rows

    def subagents(self, of: str, depth: int = 1) -> list[dict[str, Any]]:
        root = self.conversations.get_conversation(of)
        if root is None or not self._accessible(root):
            raise SourceError(f"no session {of!r}")
        rows: list[dict[str, Any]] = []
        frontier, seen, level = [root.id], {root.id}, 0
        while frontier and level < depth and self._check_deadline():
            level += 1
            children: list[Conversation] = []
            for parent_id in frontier:
                for child in self._scan(kind=None, parent_conversation_id=parent_id):
                    if child.id not in seen:
                        seen.add(child.id)
                        children.append(child)
            rows.extend({**row, "depth": level} for row in self.session_rows(children))
            frontier = [child.id for child in children]
        return rows

    def _accessible(self, conv: Conversation) -> bool:
        page = self.conversations.list_conversations(
            limit=1,
            kind=None,
            accessible_by=self.user_id,
            root_conversation_id=conv.root_conversation_id,
            include_archived=True,
        )
        return bool(page.data)

    def roles(self, handle: str | None = None, held: bool | None = None) -> list[dict[str, Any]]:
        rows = []
        for role in self._owned_roles():
            row_handle = self._role_handle(role)
            if handle is not None and row_handle != handle:
                continue
            holder = role.holder_session_id
            if held is not None and (holder is not None) != held:
                continue
            rows.append(
                {
                    "id": role.id,
                    "handle": row_handle,
                    "permanent": bool(role.permanent),
                    "holder_actor_id": holder,
                    "holder_hub_ref": self._hub_ref("session", holder) if holder else None,
                    "last_event_ts": ms(role.updated_at or role.held_since or role.created_at),
                }
            )
        return rows

    def threads(
        self, kind: str | None = None, member: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        roles = self._owned_roles()
        role_ids = {self._role_handle(role): role.id for role in roles}
        rows: list[dict[str, Any]] = []
        if kind in (None, "group"):
            rows.extend(self._group_thread_rows(role_ids))
        if kind in (None, "role_thread"):
            rows.extend(self._role_thread_rows(roles, role_ids))
        filtered = []
        for row in rows:
            if state is not None and row["state"] != state:
                continue
            if member is not None and not any(
                member in (m["handle"], m["address_id"], m["actor_id"]) for m in row["members"]
            ):
                continue
            filtered.append(row)
        return filtered

    def _group_thread_rows(self, role_ids: dict[str, str]) -> list[dict[str, Any]]:
        store = getattr(self.projects, "group_threads", None)
        if store is None:
            return []
        rows = []
        for thread in store.list_owned(user_id=self.user_id):
            members = [
                {
                    "address_id": member.id,
                    "kind": member.kind,
                    "handle": member.ref if member.kind == "role" else None,
                    "actor_id": member.ref if member.kind == "session" else None,
                    "role_id": role_ids.get(member.ref) if member.kind == "role" else None,
                }
                for member in store.members(thread.id)
            ]
            members.sort(key=lambda m: (m["handle"] or "", m["address_id"]))
            rows.append(
                {
                    "id": thread.id,
                    "hub_ref": self._hub_ref("thread", thread.id),
                    "kind": "group",
                    "title": thread.title,
                    "state": thread.state,
                    "members": members,
                    "messages": thread.last_seq,
                    "last_event_ts": ms(thread.updated_at or thread.created_at),
                }
            )
        return rows

    def _role_thread_rows(
        self, roles: list[Any], role_ids: dict[str, str]
    ) -> list[dict[str, Any]]:
        """One row per role pair, read from each holder's thread index; no hub_ref."""
        rows: dict[str, dict[str, Any]] = {}
        for role in roles:
            if not role.holder_session_id or not self._check_deadline():
                continue
            holder = self.conversations.get_conversation(role.holder_session_id)
            if holder is None:
                continue
            own = self._role_handle(role)
            for entry in thread_index(holder.labels or {}):
                pair = sorted([own, entry["counterpart"]])
                key = "role_thread:" + "|".join(pair)
                ts = ms(entry["updated_at"] or None)
                known = rows.get(key)
                if known is not None:
                    known["messages"] = max(known["messages"], entry["messages"])
                    if ts and (known["last_event_ts"] or 0) < ts:
                        known["last_event_ts"], known["state"] = ts, entry["state"]
                    continue
                rows[key] = {
                    "id": key,
                    "hub_ref": None,
                    "kind": "role_thread",
                    "title": None,
                    "state": entry["state"],
                    "members": [
                        {
                            "address_id": f"role:{handle}",
                            "kind": "role",
                            "handle": handle,
                            "actor_id": None,
                            "role_id": role_ids.get(handle),
                        }
                        for handle in pair
                    ],
                    "messages": entry["messages"],
                    "last_event_ts": ts,
                }
        return list(rows.values())

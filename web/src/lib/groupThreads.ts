import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** A group thread between the owner and several roles or sessions. */
export interface GroupThread {
  id: string;
  /** External key, `grp_<id>`; what agents pass as `thread`. */
  key: string;
  title: string;
  summary: string;
  state: "open" | "done" | string;
  lastSeq: number;
  createdAt: number;
  updatedAt: number | null;
}

export type GroupThreadMemberKind = "role" | "session";

/** A member: a role handle resolved to its holder at delivery, or a session. */
export interface GroupThreadMember {
  id: string;
  kind: GroupThreadMemberKind;
  ref: string;
  /** `vacant` when nobody holds the role, or the session is gone. */
  state: "held" | "vacant";
  sessionId: string | null;
  deliveredSeq: number;
}

export interface GroupThreadPost {
  id: string;
  seq: number;
  authorKind: "owner" | "role" | "session";
  authorRef: string;
  text: string;
  createdAt: number;
  mentions: string[];
}

/** What a late joiner is shown on arrival. */
export type GroupThreadPrimer = "summary" | "full" | "none";

/** Who a post gives a turn: everyone, or the named member refs. */
export type GroupThreadTo = "everyone" | string[];

interface WireThread {
  id: string;
  key: string;
  title: string;
  summary: string;
  state: string;
  last_seq: number;
  created_at: number;
  updated_at: number | null;
}

interface WireMember {
  id: string;
  kind: GroupThreadMemberKind;
  ref: string;
  state: "held" | "vacant";
  session_id: string | null;
  delivered_seq: number;
}

interface WirePost {
  id: string;
  seq: number;
  author_kind: GroupThreadPost["authorKind"];
  author_ref: string;
  text: string;
  created_at: number;
  mentions?: string[];
}

const BASE = "/v1/sessions/group-threads";
/** The newest posts a thread view loads; the server caps a page at 200. */
export const POSTS_PAGE = 200;

function toThread(wire: WireThread): GroupThread {
  return {
    id: wire.id,
    key: wire.key,
    title: wire.title,
    summary: wire.summary,
    state: wire.state,
    lastSeq: wire.last_seq,
    createdAt: wire.created_at,
    updatedAt: wire.updated_at,
  };
}

function toMember(wire: WireMember): GroupThreadMember {
  return {
    id: wire.id,
    kind: wire.kind,
    ref: wire.ref,
    state: wire.state,
    sessionId: wire.session_id,
    deliveredSeq: wire.delivered_seq,
  };
}

function toPost(wire: WirePost): GroupThreadPost {
  return {
    id: wire.id,
    seq: wire.seq,
    authorKind: wire.author_kind,
    authorRef: wire.author_ref,
    text: wire.text,
    createdAt: wire.created_at,
    mentions: wire.mentions ?? [],
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authenticatedFetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

const SESSION_ID = /^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A typed member: a session id is a session, anything else a `project/Role` handle. */
export function memberFromText(text: string): { kind: GroupThreadMemberKind; ref: string } {
  const ref = text.trim();
  return { kind: SESSION_ID.test(ref) ? "session" : "role", ref };
}

/** The Threads page with a new thread's members filled in, e.g. a role to start from. */
export function newThreadPath(refs: readonly string[]): string {
  const params = new URLSearchParams();
  for (const ref of refs) params.append("with", ref);
  return `/threads?${params}`;
}

/** How a member is named in the UI: the role handle, or a short session id. */
export function memberLabel(member: Pick<GroupThreadMember, "kind" | "ref">): string {
  return member.kind === "role" ? member.ref : `session ${member.ref.slice(0, 8)}`;
}

/** Who wrote a post, as shown above it. */
export function postAuthorLabel(post: GroupThreadPost): string {
  if (post.authorKind === "owner") return "You";
  return memberLabel({ kind: post.authorKind, ref: post.authorRef });
}

export async function listGroupThreads(): Promise<GroupThread[]> {
  const body = await request<{ threads: WireThread[] }>(BASE);
  return body.threads.map(toThread);
}

export async function fetchGroupThread(
  key: string,
): Promise<{ thread: GroupThread; members: GroupThreadMember[] }> {
  const body = await request<{ thread: WireThread; members: WireMember[] }>(
    `${BASE}/${encodeURIComponent(key)}`,
  );
  return { thread: toThread(body.thread), members: body.members.map(toMember) };
}

export async function fetchGroupThreadPosts(key: string): Promise<GroupThreadPost[]> {
  const params = new URLSearchParams({ limit: String(POSTS_PAGE) });
  const body = await request<{ posts: WirePost[] }>(
    `${BASE}/${encodeURIComponent(key)}/posts?${params}`,
  );
  return body.posts.map(toPost);
}

export async function createGroupThread(input: {
  title: string;
  members: { kind: GroupThreadMemberKind; ref: string }[];
  message?: string;
}): Promise<GroupThread> {
  const body = await request<{ thread: WireThread }>(BASE, {
    method: "POST",
    body: JSON.stringify({ ...input, message: input.message || undefined }),
  });
  return toThread(body.thread);
}

export async function postToGroupThread(
  key: string,
  message: string,
  to: GroupThreadTo,
): Promise<{ delivered: number }> {
  const body = await request<{ delivered: unknown[] }>(`${BASE}/${encodeURIComponent(key)}/posts`, {
    method: "POST",
    body: JSON.stringify({ message, to }),
  });
  return { delivered: body.delivered.length };
}

export async function inviteToGroupThread(
  key: string,
  member: { kind: GroupThreadMemberKind; ref: string },
  primer: GroupThreadPrimer,
): Promise<GroupThreadMember> {
  const body = await request<{ member: WireMember }>(`${BASE}/${encodeURIComponent(key)}/members`, {
    method: "POST",
    body: JSON.stringify({ ...member, primer }),
  });
  return toMember(body.member);
}

export async function removeFromGroupThread(key: string, memberId: string): Promise<void> {
  await request(`${BASE}/${encodeURIComponent(key)}/members/${encodeURIComponent(memberId)}`, {
    method: "DELETE",
  });
}

export async function updateGroupThread(
  key: string,
  changes: { title?: string; summary?: string; state?: "open" | "done" },
): Promise<GroupThread> {
  const body = await request<{ thread: WireThread }>(`${BASE}/${encodeURIComponent(key)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  return toThread(body.thread);
}

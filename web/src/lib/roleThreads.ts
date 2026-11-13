import type { StateTone } from "@/components/StateDot";
import { authenticatedFetch } from "@/lib/identity";
import { ALIAS_LABEL_KEY } from "@/lib/rolesApi";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** One row of a role holder's thread index, read from its `thread.<hash>.*` labels. */
export interface RoleThread {
  /** The `<hash>` in this thread's `thread.<hash>.*` label keys. */
  key: string;
  counterpart: string;
  state: string;
  unread: number;
  messages: number;
  summary: string;
  updatedAt: number;
  /** Messages dropped because the role had no holder when their session was deleted. */
  dropped?: number;
  /** A message the loop guard is holding back from delivery, if any. */
  held?: HeldThreadMessage;
}

/** A thread message withheld by the loop guard until the owner resolves it. */
export interface HeldThreadMessage {
  text: string;
  /** Session id the message was addressed to. */
  to: string | null;
  /** Epoch seconds when it was held. */
  at: number | null;
}

const INDEX_LABEL = /^thread\.([0-9a-f]{12})\.([a-z_]+)$/;
// Mirrors omnigent/server/role_threads.py: needs the owner or blocked, then our turn, then the rest.
const URGENT_STATES: Record<string, number> = {
  waiting_on_owner: 0,
  blocked: 0,
  waiting_on_us: 1,
  no_reply: 1,
};

/** States that call for attention in the sidebar: the state word is shown, not just its dot. */
export function roleThreadIsUrgent(state: string): boolean {
  return state in URGENT_STATES;
}

/** Mirrors `thread_needs_owner` on the server: only a held message or an explicit mark. */
export function roleThreadNeedsOwner(thread: RoleThread): boolean {
  return thread.state === "waiting_on_owner" || thread.held !== undefined;
}

/** How many threads a role has, and how many of them need the owner. */
export function roleThreadCounts(threads: RoleThread[]): { total: number; needsYou: number } {
  return { total: threads.length, needsYou: threads.filter(roleThreadNeedsOwner).length };
}

/** The tooltip and accessible text for a role's thread count: "7 threads, 2 need you". */
export function roleThreadCountText({ total, needsYou }: { total: number; needsYou: number }) {
  const threads = total === 1 ? "1 thread" : `${total} threads`;
  if (needsYou === 0) return threads;
  return `${threads}, ${needsYou} ${needsYou === 1 ? "needs" : "need"} you`;
}

function toInt(value: string | undefined): number {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** The thread index of a session holding a role; empty for any other session. */
export function roleThreadsFromLabels(
  labels: Record<string, string> | null | undefined,
): RoleThread[] {
  if (!labels?.[ALIAS_LABEL_KEY]?.trim()) return [];
  const grouped = new Map<string, Record<string, string>>();
  for (const [key, value] of Object.entries(labels)) {
    const match = INDEX_LABEL.exec(key);
    if (!match) continue;
    const fields = grouped.get(match[1]) ?? {};
    fields[match[2]] = value;
    grouped.set(match[1], fields);
  }
  const rows: RoleThread[] = [];
  for (const [key, fields] of grouped) {
    if (!fields.cp) continue;
    const held = fields.held
      ? {
          text: fields.held,
          to: fields.held_to || null,
          at: fields.held_at ? toInt(fields.held_at) : null,
        }
      : undefined;
    rows.push({
      key,
      counterpart: fields.cp,
      state: fields.state ?? "open",
      unread: toInt(fields.unread),
      messages: toInt(fields.n),
      summary: fields.sum ?? "",
      updatedAt: toInt(fields.at),
      dropped: toInt(fields.dropped),
      ...(held ? { held } : {}),
    });
  }
  return rows.sort(
    (a, b) =>
      (URGENT_STATES[a.state] ?? 2) - (URGENT_STATES[b.state] ?? 2) || b.updatedAt - a.updatedAt,
  );
}

const STATE_TEXT: Record<string, string> = {
  open: "Open",
  waiting_on_them: "Their turn",
  waiting_on_us: "Our turn",
  waiting_on_owner: "Needs you",
  blocked: "Blocked",
  no_reply: "No reply",
  done: "Done",
};

const STATE_TONE: Record<string, StateTone> = {
  open: "neutral",
  waiting_on_them: "blue",
  waiting_on_us: "amber",
  waiting_on_owner: "yellow",
  blocked: "red",
  no_reply: "amber",
  done: "green",
};

/** Human text for a thread state; unknown states show as sent. */
export function roleThreadStateText(state: string): string {
  return STATE_TEXT[state] ?? state;
}

const STATE_DESCRIPTION: Record<string, string> = {
  waiting_on_us: "The last message came in to us and hasn't been answered or closed.",
  waiting_on_them: "We sent the last message and expect a reply.",
  open: "Nobody owes a reply; the thread stays available.",
  waiting_on_owner: "The roles are waiting for your answer or decision.",
  blocked: "Work can't continue until something outside this thread changes.",
  no_reply: "The role left this thread unhandled through its reminders, or stayed offline.",
  done: "The thread is finished; nothing further is expected.",
};

/** What a thread state means, for its tooltip; empty for unknown states. */
export function roleThreadStateDescription(state: string): string {
  return STATE_DESCRIPTION[state] ?? "";
}

/**
 * Labels that set a thread's state from the web, as `note_thread` in
 * omnigent/server/role_threads.py does: closing or handing over clears unread.
 */
export function roleThreadStateLabels(
  thread: RoleThread,
  state: string,
  now = Math.floor(Date.now() / 1000),
): Record<string, string> {
  const labels: Record<string, string> = {
    [`thread.${thread.key}.state`]: state,
    [`thread.${thread.key}.at`]: String(now),
  };
  if (state === "done" || state === "waiting_on_them") labels[`thread.${thread.key}.unread`] = "0";
  return labels;
}

/** The namespace path of a role handle: `forthkit/Product` → `forthkit`, `/Bench` → `""`. */
export function roleHandlePath(handle: string): string {
  return handle.slice(0, Math.max(0, handle.lastIndexOf("/")));
}

/** Colour of a thread state's mark; unknown states are neutral. */
export function roleThreadStateTone(state: string): StateTone {
  return STATE_TONE[state] ?? "neutral";
}

/** Who wrote a thread message: `in` the counterpart, `out` the role, `owner` you, `hub` a reminder. */
export type RoleThreadDirection = "in" | "out" | "owner" | "hub";

/** One message of a thread, oldest first in a thread view. */
export interface RoleThreadMessage {
  direction: RoleThreadDirection;
  sessionId: string;
  itemId: string;
  createdAt: number;
  text: string;
  /** Whom an owner post reached; absent on other messages. */
  to?: RoleThreadPostTarget;
  /** Sent to a role nobody holds; delivered when someone takes the role. */
  waitingHolder?: boolean;
}

interface WireThreadMessage {
  direction: RoleThreadDirection;
  session_id: string;
  item_id: string;
  created_at: number;
  text: string;
  to?: RoleThreadPostTarget;
  waiting_holder?: boolean;
}

/** `GET /v1/sessions/{id}/threads/messages`: both sides of one thread, oldest first. */
export async function fetchRoleThreadMessages(
  holderId: string,
  counterpart: string,
): Promise<RoleThreadMessage[]> {
  const params = new URLSearchParams({ counterpart });
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(holderId)}/threads/messages?${params}`,
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { messages?: WireThreadMessage[] };
  return (body.messages ?? []).map((m) => ({
    direction: m.direction,
    sessionId: m.session_id,
    itemId: m.item_id,
    createdAt: m.created_at,
    text: m.text,
    ...(m.to ? { to: m.to } : {}),
    ...(m.waiting_holder ? { waitingHolder: true } : {}),
  }));
}

/** Who an owner post goes to: both roles, only the thread's holder, or only its counterpart. */
export type RoleThreadPostTarget = "both" | "holder" | "counterpart";

/** `POST /v1/sessions/{id}/threads/post`: the owner's words, verbatim, into the thread. */
export async function postRoleThreadMessage(
  holderId: string,
  counterpart: string,
  message: string,
  to: RoleThreadPostTarget,
): Promise<{ messageId: string; delivered: string[] }> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(holderId)}/threads/post`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ counterpart, message, to }),
    },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { message_id: string; delivered: string[] };
  return { messageId: body.message_id, delivered: body.delivered };
}

/** What the owner does with a held message: deliver it, let the pair continue, or drop it. */
export type HeldThreadAction = "release" | "allow" | "discard";

/** `POST /v1/sessions/{id}/threads/held`: resolve the message the loop guard is holding. */
export async function resolveHeldThreadMessage(
  holderId: string,
  counterpart: string,
  action: HeldThreadAction,
): Promise<unknown> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(holderId)}/threads/held`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ counterpart, action }),
    },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
  return res.json();
}

const POST_FOCUS_EVENT = "omnigent:role-thread-post-focus";
let pendingPostFocus = false;

/** Asks the thread composer to take focus, now or as soon as it mounts. */
export function requestRoleThreadPostFocus(): void {
  pendingPostFocus = true;
  window.dispatchEvent(new Event(POST_FOCUS_EVENT));
}

/** Runs `focus` for a pending or future focus request; returns the unsubscribe. */
export function onRoleThreadPostFocus(focus: () => void): () => void {
  const handle = () => {
    if (!pendingPostFocus) return;
    pendingPostFocus = false;
    focus();
  };
  handle();
  window.addEventListener(POST_FOCUS_EVENT, handle);
  return () => window.removeEventListener(POST_FOCUS_EVENT, handle);
}

/** The reminder a nudge sends the role: what's unhandled, from whom, and for how long. */
export function roleThreadNudgeText(
  thread: RoleThread,
  now = Math.floor(Date.now() / 1000),
): string {
  const count = thread.unread === 1 ? "1 message" : `${thread.unread} messages`;
  const minutes = thread.updatedAt > 0 ? Math.max(0, Math.round((now - thread.updatedAt) / 60)) : 0;
  const since = minutes > 0 ? `, waiting ${minutes} min` : "";
  return (
    `Reminder: ${count} from ${thread.counterpart} in this thread still unhandled${since}. ` +
    "Please reply or act on them, then record the thread with sys_thread_note."
  );
}

/** Nudges the thread's holder alone, as an owner post into the thread. */
export function nudgeRoleThread(holderId: string, thread: RoleThread): Promise<unknown> {
  return postRoleThreadMessage(holderId, thread.counterpart, roleThreadNudgeText(thread), "holder");
}

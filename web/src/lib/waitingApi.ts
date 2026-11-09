import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type WaitingKind =
  | "tool_call"
  | "async_task"
  | "background_shell"
  | "subagent"
  | "approval"
  | "queued_message"
  | "thread"
  | "runner_offline";

export type WaitingAction = "open" | "nudge" | "stop";

/** One thing a session is waiting on, as `GET /v1/sessions/{id}/waiting` returns it. */
export interface WaitingItem {
  kind: WaitingKind;
  session_id: string;
  title: string;
  detail?: string | null;
  /** Epoch seconds the wait began. */
  since: number | null;
  where?: string | null;
  ref?: string | null;
  stale: boolean;
  actions: WaitingAction[];
}

export interface SessionWaiting {
  session_id: string;
  title: string | null;
  status: "idle" | "running" | "waiting" | "failed";
  /** Epoch seconds of the last status transition. */
  status_since: number | null;
  waiting_reason: string | null;
  host_id?: string | null;
  runner_online?: boolean | null;
  items: WaitingItem[];
}

/** `GET /v1/sessions/{id}/waiting` */
export async function fetchSessionWaiting(sessionId: string): Promise<SessionWaiting> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/waiting`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as SessionWaiting;
  return { ...body, items: body.items ?? [] };
}

/** `GET /v1/sessions/waiting`: running/waiting sessions and any with items. */
export async function fetchAllWaiting(): Promise<SessionWaiting[]> {
  const res = await authenticatedFetch("/v1/sessions/waiting");
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { sessions?: SessionWaiting[] };
  return (body.sessions ?? []).map((s) => ({ ...s, items: s.items ?? [] }));
}

/** Compact age of a wait: "45 s", "12 min", "3 h", "2 d". Epoch seconds in. */
export function formatWaitAge(since: number, now = Math.floor(Date.now() / 1000)): string {
  const seconds = Math.max(0, Math.floor(now - since));
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

/** Number of stale items across sessions. */
export function countStaleItems(sessions: readonly SessionWaiting[]): number {
  return sessions.reduce((sum, s) => sum + s.items.filter((i) => i.stale).length, 0);
}

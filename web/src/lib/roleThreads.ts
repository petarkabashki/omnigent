import type { StateTone } from "@/components/StateDot";
import { authenticatedFetch } from "@/lib/identity";
import { ALIAS_LABEL_KEY } from "@/lib/rolesApi";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** One row of a role holder's thread index, read from its `thread.<hash>.*` labels. */
export interface RoleThread {
  counterpart: string;
  state: string;
  unread: number;
  messages: number;
  summary: string;
  updatedAt: number;
}

const INDEX_LABEL = /^thread\.([0-9a-f]{12})\.([a-z]+)$/;
// Mirrors omnigent/server/role_threads.py: blocked, then waiting_on_us, then the rest.
const URGENT_STATES: Record<string, number> = { blocked: 0, waiting_on_us: 1 };

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
  for (const fields of grouped.values()) {
    if (!fields.cp) continue;
    rows.push({
      counterpart: fields.cp,
      state: fields.state ?? "open",
      unread: toInt(fields.unread),
      messages: toInt(fields.n),
      summary: fields.sum ?? "",
      updatedAt: toInt(fields.at),
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
  blocked: "Blocked",
  done: "Done",
};

const STATE_TONE: Record<string, StateTone> = {
  open: "neutral",
  waiting_on_them: "blue",
  waiting_on_us: "amber",
  blocked: "red",
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
  blocked: "Work can't continue until something outside this thread changes.",
  done: "The thread is finished; nothing further is expected.",
};

/** What a thread state means, for its tooltip; empty for unknown states. */
export function roleThreadStateDescription(state: string): string {
  return STATE_DESCRIPTION[state] ?? "";
}

/** Colour of a thread state's mark; unknown states are neutral. */
export function roleThreadStateTone(state: string): StateTone {
  return STATE_TONE[state] ?? "neutral";
}

/** One message of a thread: `in` from the counterpart, `out` sent by the role. */
export interface RoleThreadMessage {
  direction: "in" | "out";
  sessionId: string;
  itemId: string;
  createdAt: number;
  text: string;
}

interface WireThreadMessage {
  direction: "in" | "out";
  session_id: string;
  item_id: string;
  created_at: number;
  text: string;
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
  }));
}

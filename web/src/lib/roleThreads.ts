import { ALIAS_LABEL_KEY } from "@/lib/rolesApi";

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
  waiting_on_them: "Waiting on them",
  waiting_on_us: "Waiting on us",
  blocked: "Blocked",
  done: "Done",
};

/** Human text for a thread state; unknown states show as sent. */
export function roleThreadStateText(state: string): string {
  return STATE_TEXT[state] ?? state;
}

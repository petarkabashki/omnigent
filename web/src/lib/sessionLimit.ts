// Sidebar marker for a session whose last turn hit the account usage limit.
// It reads the raw failure labels because list rows do not carry
// `last_task_error`. A marker with no reset time stays until the labels clear.

import { useNowSelector } from "@/hooks/useNow";

const CODE_LABEL = "omnigent.last_task_error_code";
const RESETS_AT_LABEL = "omnigent.last_task_error_resets_at";
const USAGE_LIMIT_CODE = "usage_limit_reached";

export interface SessionLimit {
  /** Epoch seconds the limit lifts, or null when the failure text had no readable reset. */
  resetsAt: number | null;
}

/**
 * The limit state for a row, or null when the row stays unmarked.
 *
 * A reset in the past means the limit has lifted, so nothing shows. An empty
 * or malformed reset (Codex, or text the server could not parse) still marks.
 */
export function sessionLimit(
  labels: Readonly<Record<string, string>> | null | undefined,
  nowMs: number,
): SessionLimit | null {
  if (labels?.[CODE_LABEL] !== USAGE_LIMIT_CODE) return null;
  const raw = labels[RESETS_AT_LABEL];
  const resetsAt = raw && /^\d+$/.test(raw) ? Number(raw) : null;
  if (resetsAt !== null && resetsAt * 1000 <= nowMs) return null;
  return { resetsAt };
}

/** The character drawn on the title line. */
export function sessionLimitGlyph(): { glyph: string; className: string } {
  return { glyph: "◔", className: "text-status-yellow" };
}

/** The tooltip sentence, with the reset time in the viewer's locale when known. */
export function sessionLimitDetail(limit: SessionLimit): string {
  if (limit.resetsAt === null) return "Usage limit reached.";
  const when = new Date(limit.resetsAt * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  return `Usage limit reached. Resets ${when}.`;
}

/**
 * The row's limit state, re-evaluated on the shared clock tick so the marker
 * clears once the reset time passes even when the row's data is unchanged.
 */
export function useSessionLimit(
  labels: Readonly<Record<string, string>> | null | undefined,
): SessionLimit | null {
  const marked = labels?.[CODE_LABEL] === USAGE_LIMIT_CODE;
  useNowSelector((now) => sessionLimit(labels, now.getTime()) !== null, { enabled: marked });
  return sessionLimit(labels, Date.now());
}

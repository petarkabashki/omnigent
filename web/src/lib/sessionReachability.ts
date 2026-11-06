// Sidebar reachability, from the same runner and host signals the open
// session folds into `useSessionLiveness`. The row only names the states a
// person can act on without opening the chat. A live runner and an unseen
// poll stay unmarked.

export type SessionReachability = "online" | "asleep" | "offline" | "unknown";

/** The list fields this derivation reads. */
export interface SessionReachabilityRow {
  runner_online?: boolean | null;
  host_online?: boolean | null;
  /** Sub-agent rows recover through the parent runner, so a down runner is asleep. */
  kind?: "default" | "sub_agent" | null;
}

/**
 * Whether the session can take a turn right now.
 *
 * `runner_online` true is online. A missing value is unknown, so a poll that
 * has not landed yet does not flash "Offline". A down runner on a live host
 * is asleep: the next message relaunches it. A down host, or a down runner
 * with no host binding (`host_online` null), is offline.
 */
export function sessionReachability(row: SessionReachabilityRow): SessionReachability {
  if (row.runner_online === true) return "online";
  if (row.runner_online !== false) return "unknown";
  if (row.kind === "sub_agent" || row.host_online === true) return "asleep";
  if (row.host_online === false || row.host_online === null) return "offline";
  return "unknown";
}

/** The short word the sidebar row announces, or null when the row stays unmarked. */
export function sessionReachabilityWord(kind: SessionReachability): "Asleep" | "Offline" | null {
  if (kind === "asleep") return "Asleep";
  if (kind === "offline") return "Offline";
  return null;
}

/** The character drawn on the title line for the same state. */
export function sessionReachabilityGlyph(
  kind: SessionReachability,
): { glyph: string; className: string } | null {
  if (kind === "asleep") return { glyph: "☾", className: "text-status-yellow" };
  if (kind === "offline") return { glyph: "⊘", className: "text-muted-foreground" };
  return null;
}

/** The sentence the tooltip uses for the same state. */
export function sessionReachabilityDetail(row: SessionReachabilityRow): string | null {
  const kind = sessionReachability(row);
  if (kind === "asleep") return "Runner is asleep. A message wakes it.";
  if (kind === "offline" && row.host_online === false) return "Host is offline.";
  if (kind === "offline") return "Agent is disconnected.";
  return null;
}

/**
 * The state line in the session popup. A live runner is named, because the
 * row leaves it unmarked. An unseen poll stays blank.
 */
export function sessionReachabilityPopup(row: SessionReachabilityRow): string | null {
  const kind = sessionReachability(row);
  if (kind === "online") return "Online";
  if (kind === "asleep") return "Asleep. A message wakes it.";
  if (kind === "offline" && row.host_online === false) return "Offline";
  if (kind === "offline") return "Disconnected";
  return null;
}

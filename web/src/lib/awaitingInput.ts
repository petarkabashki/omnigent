// "Awaiting your input": a session blocked on the user, either through a formal
// approval dialog (pending elicitations) or a plain request the server flags in
// the `omnigent.awaiting_input` label (an agent question, a native TUI prompt).
// Every "Needs response" surface reads these helpers so they agree.

export const AWAITING_INPUT_LABEL_KEY = "omnigent.awaiting_input";

/** The row fields the predicate reads; every row shape (session, child) fits. */
export interface AwaitingInputFields {
  pending_elicitations_count?: number | null;
  labels?: Record<string, string>;
  runner_online?: boolean | null;
}

/** The server-set reason the session waits on the user, or `undefined`. */
export function awaitingInputReason(session: AwaitingInputFields): string | undefined {
  // A dead runner can't be answered; the server zeroes its approval count too.
  if (session.runner_online === false) return undefined;
  return session.labels?.[AWAITING_INPUT_LABEL_KEY] || undefined;
}

/** Whether the session needs a response from the user, by either signal. */
export function needsResponse(session: AwaitingInputFields): boolean {
  return (
    (session.pending_elicitations_count ?? 0) > 0 || awaitingInputReason(session) !== undefined
  );
}

/** Outstanding prompts: approvals plus one for a plain awaiting-input request. */
export function needsResponseCount(session: AwaitingInputFields): number {
  return (session.pending_elicitations_count ?? 0) + (awaitingInputReason(session) ? 1 : 0);
}

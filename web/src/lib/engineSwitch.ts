import { isNativeHarness } from "./forkHarness";

// Native harnesses the server can reset in place (IN_PLACE_RESET_NATIVE_HARNESSES).
const RESETTABLE_NATIVE_HARNESSES = new Set([
  "claude-native",
  "native-claude",
  "codex-native",
  "native-codex",
]);

/**
 * Whether a session on `harness` can be reset onto another engine in place:
 * SDK harnesses and Claude/Codex native. The server remains the authority and
 * refuses the rest with RESET_UNSUPPORTED; this only decides whether to offer it.
 */
export function harnessSupportsEngineSwitch(harness: string | null | undefined): boolean {
  if (!harness) return false;
  if (RESETTABLE_NATIVE_HARNESSES.has(harness)) return true;
  return !(isNativeHarness(harness) || harness.endsWith("-native"));
}

/** What the server wants confirmed before it will switch: queued input, a running turn, or live work. */
export type SwitchRefusal = "pending" | "running" | "work";

/** Classify a 409 reset refusal by its error code and the override its message names. */
export function switchRefusalOf(err: unknown): SwitchRefusal | null {
  const { code, message } = (err ?? {}) as { code?: unknown; message?: unknown };
  const text = typeof message === "string" ? message : "";
  const lower = typeof code === "string" ? code.toLowerCase() : "";
  if (lower === "pending_inputs") return "pending";
  if (lower !== "session_busy") return null;
  if (text.includes("acknowledge_running_work")) return "work";
  if (text.includes("stop_running")) return "running";
  return null;
}

/** Plain-language cost of each override, shown before the user confirms it. */
export const SWITCH_REFUSAL_COPY: Record<SwitchRefusal, { warning: string; action: string }> = {
  pending: {
    warning:
      "This session has messages queued that haven't been sent yet. Switching now discards them.",
    action: "Discard queued messages and switch",
  },
  running: {
    warning: "This session is still running a turn. Switching now stops it mid-way.",
    action: "Stop the turn and switch",
  },
  work: {
    warning:
      "This session still shows running sub-agents or background tasks. If you switch, any that are really running are left behind and their results may be lost.",
    action: "Switch anyway",
  },
};

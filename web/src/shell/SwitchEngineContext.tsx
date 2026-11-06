// Opener for the switch-engine dialog. AppShell owns the dialog; the composer's
// model selector and the usage-limit error card open it through this context.

import { createContext, useContext } from "react";

export interface SwitchEngineContextValue {
  /** Session the dialog acts on; error cards in other scopes (side chats) hide the action. */
  currentSessionId: string | null;
  /** The open session is owned by the viewer, top-level, and on a harness that can reset in place. */
  canSwitchEngine: boolean;
  openSwitchEngine: () => void;
}

const SwitchEngineContext = createContext<SwitchEngineContextValue | null>(null);

export const SwitchEngineContextProvider = SwitchEngineContext.Provider;

/** `null` outside AppShell (isolated tests); callers hide the affordance. */
export function useSwitchEngine(): SwitchEngineContextValue | null {
  return useContext(SwitchEngineContext);
}

// Whether the server has stayed out of reach long enough to tell the user.

import { useEffect, useState } from "react";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { useSessionUpdatesConnected } from "./useSessionUpdatesConnected";

/** How long the stream must stay down before the app says so; a shorter blip shows nothing. */
export const SERVER_UNREACHABLE_DELAY_MS = 5_000;

/**
 * `true` once the session-updates stream has been down for
 * {@link SERVER_UNREACHABLE_DELAY_MS} after having worked, and `false` again the
 * moment it reconnects. A half-open connection only counts as down once the
 * socket's silence watchdog closes it, so it is reported that much later.
 *
 * @param delayMs - Override of the debounce, for tests.
 */
export function useServerUnreachable(delayMs: number = SERVER_UNREACHABLE_DELAY_MS): boolean {
  const connected = useSessionUpdatesConnected();
  const [unreachable, setUnreachable] = useState(false);
  useEffect(() => {
    if (connected || !sessionUpdatesSocket.hasConnected()) {
      setUnreachable(false);
      return;
    }
    const timer = setTimeout(() => setUnreachable(true), delayMs);
    return () => clearTimeout(timer);
  }, [connected, delayMs]);
  return unreachable;
}

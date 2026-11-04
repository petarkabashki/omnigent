import { WifiOffIcon } from "lucide-react";
import { useServerUnreachable } from "@/hooks/useServerUnreachable";

/** App-wide notice shown while the server cannot be reached; clears itself on reconnect. */
export function ServerUnreachableBanner() {
  if (!useServerUnreachable()) return null;
  return (
    <div
      role="status"
      className="pointer-events-none fixed inset-x-2 top-2 z-50 flex justify-center"
    >
      <div className="pointer-events-auto flex max-w-xl items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-ui text-foreground shadow-md backdrop-blur">
        <WifiOffIcon className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
        <p>
          <span className="font-medium">Can't reach the server.</span> Omnigent keeps retrying and
          this goes away once it's back. Messages can't be sent until then; what you've typed stays
          in the box.
        </p>
      </div>
    </div>
  );
}

import { WifiOffIcon } from "lucide-react";
import { useLayoutEffect, useRef } from "react";
import { useServerUnreachable } from "@/hooks/useServerUnreachable";

/** App-wide notice shown while the server cannot be reached; clears itself on reconnect. */
export function ServerUnreachableBanner() {
  const unreachable = useServerUnreachable();
  const bannerRef = useRef<HTMLDivElement>(null);
  // Publish the banner's height so the app shell reserves that strip and no
  // control sits underneath it.
  useLayoutEffect(() => {
    const banner = bannerRef.current;
    if (!unreachable || !banner) return;
    const root = document.documentElement;
    const publish = () =>
      root.style.setProperty("--server-banner-height", `${banner.offsetHeight}px`);
    publish();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(publish);
    observer?.observe(banner);
    return () => {
      observer?.disconnect();
      root.style.removeProperty("--server-banner-height");
    };
  }, [unreachable]);
  if (!unreachable) return null;
  return (
    <div
      ref={bannerRef}
      role="status"
      className="pointer-events-none fixed inset-x-0 top-0 z-50 flex justify-center p-2"
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

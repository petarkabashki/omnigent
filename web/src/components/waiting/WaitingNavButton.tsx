import type { MouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { HourglassIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { countStaleItems, fetchAllWaiting } from "@/lib/waitingApi";

/** Query key shared by the sidebar badge and the Waiting page. */
export const ALL_WAITING_QUERY_KEY = ["waiting", "all"] as const;

/** Sidebar entry for the Waiting page; the badge counts stale items and hides at zero. */
export function WaitingNavButton({
  active,
  onNavClick,
}: {
  active: boolean;
  onNavClick?: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const { data } = useQuery({
    queryKey: ALL_WAITING_QUERY_KEY,
    queryFn: fetchAllWaiting,
    refetchInterval: 30_000,
    retry: false,
  });
  const stale = data ? countStaleItems(data) : 0;
  return (
    <Button
      asChild
      variant="ghost"
      className={cn(
        "w-full justify-start border-0 font-normal mobile-touch-target",
        active && "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)]",
      )}
      data-testid="waiting-button"
    >
      <Link to="/waiting" onClick={onNavClick} componentId="sidebar.waiting">
        <HourglassIcon
          className={cn(
            "ui-icon",
            active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
          )}
        />
        Waiting
        {stale > 0 && (
          <span
            aria-label={`${stale} stale waiting ${stale === 1 ? "item" : "items"}`}
            className="ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500/20 px-1 text-10 font-medium text-amber-700 tabular-nums dark:text-amber-300"
          >
            {stale}
          </span>
        )}
      </Link>
    </Button>
  );
}

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { StateDot, type StateTone } from "@/components/StateDot";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fetchSessionWaiting, formatWaitAge } from "@/lib/waitingApi";
import { WaitingItemRow } from "./WaitingItemRow";
import { useWaitingActions } from "./useWaitingActions";

export interface WaitingStatusChipProps {
  sessionId: string;
  status: string | null | undefined;
  statusSince?: number | null;
  waitingReason?: string | null;
}

/** The text of the chip: "Running 12 min" or "Waiting 12 min · 3 background shells". */
export function waitingChipLabel(
  status: string,
  since: number,
  reason: string | null | undefined,
  now?: number,
): string {
  // The snapshot reports a parked session as running; a reason marks it waiting.
  const waiting = status === "waiting" || Boolean(reason);
  const age = formatWaitAge(since, now);
  return waiting ? `Waiting ${age}${reason ? ` · ${reason}` : ""}` : `Running ${age}`;
}

/** Header chip: how long the session has been running or waiting; opens what it waits on. */
export function WaitingStatusChip({
  sessionId,
  status,
  statusSince,
  waitingReason,
}: WaitingStatusChipProps) {
  const [open, setOpen] = useState(false);
  const actions = useWaitingActions();
  const { data } = useQuery({
    queryKey: ["waiting", "session", sessionId],
    queryFn: () => fetchSessionWaiting(sessionId),
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  });
  if ((status !== "running" && status !== "waiting") || !statusSince) return null;
  const tone: StateTone = status === "waiting" || waitingReason ? "amber" : "blue";
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="waiting-status-chip"
          className="inline-flex max-w-64 shrink-0 items-center gap-1.5 truncate rounded-full px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <StateDot tone={tone} />
          <span className="truncate">{waitingChipLabel(status, statusSince, waitingReason)}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96 max-w-[calc(100vw-2rem)]">
        {data && data.items.length > 0 ? (
          <ul className="divide-y divide-border">
            {data.items.map((item, index) => (
              <WaitingItemRow key={`${item.kind}-${item.ref ?? index}`} item={item} {...actions} />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">
            {data ? "Nothing specific is being waited on." : "Loading..."}
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}

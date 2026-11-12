import { useQuery } from "@tanstack/react-query";
import { StateDot } from "@/components/StateDot";
import { ALL_WAITING_QUERY_KEY } from "@/components/waiting/WaitingNavButton";
import { WaitingItemRow } from "@/components/waiting/WaitingItemRow";
import { useWaitingActions } from "@/components/waiting/useWaitingActions";
import { waitingChipLabel } from "@/components/waiting/WaitingStatusChip";
import {
  countOwnerItems,
  fetchAllWaiting,
  sessionContext,
  type SessionWaiting,
} from "@/lib/waitingApi";

function sessionAge(session: SessionWaiting): number {
  const times = [session.status_since, ...session.items.map((i) => i.since)].filter(
    (t): t is number => t != null,
  );
  return times.length > 0 ? Math.min(...times) : Number.MAX_SAFE_INTEGER;
}

/** Waiting page (`/waiting`): every running or waiting session and what it waits on, oldest first. */
export function WaitingPage() {
  const actions = useWaitingActions();
  const { data, isLoading, error } = useQuery({
    queryKey: ALL_WAITING_QUERY_KEY,
    queryFn: fetchAllWaiting,
    refetchInterval: 30_000,
  });
  const sessions = [...(data ?? [])].sort((a, b) => sessionAge(a) - sessionAge(b));
  const needed = countOwnerItems(sessions);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-16">
      <header>
        <h1 className="text-lg font-semibold">Waiting</h1>
        <p data-testid="waiting-owner-count" className="text-sm text-muted-foreground">
          {needed === 0
            ? "Nothing needs you."
            : `${needed} ${needed === 1 ? "item needs" : "items need"} you.`}
        </p>
      </header>
      {isLoading && <p className="text-sm text-muted-foreground">Loading...</p>}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error instanceof Error ? error.message : "Could not load waiting sessions."}
        </p>
      )}
      {data && sessions.length === 0 && (
        <p className="text-sm text-muted-foreground">No session is waiting on anything.</p>
      )}
      {sessions.map((session) => (
        <section
          key={session.session_id}
          data-testid="waiting-session"
          className="rounded-lg border border-border p-3"
        >
          <div className="flex items-center gap-2">
            <StateDot tone={session.status === "waiting" ? "amber" : "blue"} />
            <button
              type="button"
              className="truncate text-left text-ui font-medium hover:underline"
              onClick={() => actions.onOpen(session.session_id)}
            >
              {sessionContext(session) && (
                <span data-testid="waiting-session-context" className="text-muted-foreground">
                  {sessionContext(session)} ·{" "}
                </span>
              )}
              {session.title || session.session_id}
            </button>
            {session.status_since != null &&
              (session.status === "running" || session.status === "waiting") && (
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {waitingChipLabel(session.status, session.status_since, session.waiting_reason)}
                </span>
              )}
          </div>
          {session.items.length > 0 && (
            <ul className="mt-1 divide-y divide-border">
              {session.items.map((item, index) => (
                <WaitingItemRow
                  key={`${item.kind}-${item.ref ?? index}`}
                  item={item}
                  {...actions}
                />
              ))}
            </ul>
          )}
        </section>
      ))}
    </main>
  );
}

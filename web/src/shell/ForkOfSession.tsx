import { useQuery } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { ALIAS_LABEL_KEY, FORK_OF_LABEL_KEY } from "@/lib/rolesApi";
import { getSessionSlim } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { UNTITLED_CONVERSATION_LABEL } from "./sidebarNav";

/** Older forks name their parent only in a side-chat or fork-source label. */
const FORK_PARENT_LABEL_KEYS = [
  FORK_OF_LABEL_KEY,
  "omnigent.side_chat.source_id",
  "omnigent.fork.source_id",
] as const;

/** The session a fork was made from, or null when the session is not a fork. */
export function forkParentId(labels: Record<string, string> | null | undefined): string | null {
  for (const key of FORK_PARENT_LABEL_KEYS) {
    const value = labels?.[key]?.trim();
    if (value) return value;
  }
  return null;
}

/**
 * "Fork of <parent>" with a link, plus the role the parent holds now. Read from
 * the parent's own access-checked snapshot, never from the fork's labels.
 */
export function ForkOfSession({ sessionId, className }: { sessionId: string; className?: string }) {
  const {
    data: parent,
    isFetchedAfterMount,
    isFetching,
    error,
  } = useQuery({
    queryKey: ["session-fork-parent", sessionId],
    queryFn: () => getSessionSlim(sessionId),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  if (!isFetchedAfterMount || isFetching) return null;
  const role = parent && !error ? (parent.labels?.[ALIAS_LABEL_KEY]?.trim() ?? "") : "";
  return (
    <span
      className={cn("min-w-0 truncate text-xs text-muted-foreground", className)}
      data-testid="fork-of-session"
    >
      Fork of{" "}
      {parent && !error ? (
        <Link
          to={`/c/${sessionId}`}
          className="hover:text-foreground hover:underline"
          title="Session this fork was made from"
        >
          {parent.title || UNTITLED_CONVERSATION_LABEL}
        </Link>
      ) : (
        "another session"
      )}
      {role && <span data-testid="fork-of-session-role"> · {role}</span>}
    </span>
  );
}

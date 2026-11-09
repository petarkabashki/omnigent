import { useQuery } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";
import { useSession } from "@/hooks/useSession";
import { fetchRoleThreadMessages, roleThreadsFromLabels } from "@/lib/roleThreads";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

/** Label on an owner-started side chat naming its role-thread counterpart. */
export const THREAD_OF_LABEL_KEY = "thread.of";
/** Label on a side chat naming the session it was opened from. */
export const SIDE_CHAT_SOURCE_LABEL_KEY = "omnigent.side_chat.source_id";

/**
 * A role thread's messages, both directions, oldest first. Rendered at the top
 * of the thread's side chat so the conversation and the reply box sit together.
 */
export function RoleThreadHistory({
  holderId,
  counterpart,
}: {
  holderId: string;
  counterpart: string;
}) {
  const { session: holder } = useSession(holderId);
  const holderName = holder?.labels?.["omnigent.alias"]?.trim() || holder?.title || "This role";
  // The thread index on the holder moves when a message lands; refetch then.
  const updatedAt = roleThreadsFromLabels(holder?.labels).find(
    (thread) => thread.counterpart === counterpart,
  )?.updatedAt;
  const messages = useQuery({
    queryKey: ["role-thread-messages", holderId, counterpart, updatedAt],
    queryFn: () => fetchRoleThreadMessages(holderId, counterpart),
    retry: 1,
  });
  return (
    <section
      data-testid="role-thread-history"
      aria-label={`Thread with ${counterpart}`}
      className="mb-4 flex flex-col gap-2 border-b border-border pb-4"
    >
      <p className="text-xs font-medium text-muted-foreground">
        {holderName} ↔ {counterpart}
      </p>
      {messages.isPending && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" /> Loading messages…
        </p>
      )}
      {messages.isError && (
        <p role="alert" className="text-sm text-destructive">
          Couldn’t load this thread’s messages.
        </p>
      )}
      {messages.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No messages recorded for this thread.</p>
      )}
      {messages.data?.map((message) => (
        <div
          key={`${message.sessionId}:${message.itemId}`}
          data-testid="role-thread-message"
          data-direction={message.direction}
          className={cn(
            "max-w-[85%] rounded-lg border border-border px-3 py-2 text-sm",
            message.direction === "out" ? "self-end bg-muted" : "self-start",
          )}
        >
          <p className="mb-1 text-xs text-muted-foreground">
            {message.direction === "out" ? holderName : counterpart} ·{" "}
            {relativeTime(message.createdAt * 1000)}
          </p>
          <p className="break-words whitespace-pre-wrap">{message.text}</p>
        </div>
      ))}
    </section>
  );
}

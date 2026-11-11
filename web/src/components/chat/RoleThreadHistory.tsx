import { useQuery } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";
import { HeldThreadMessage } from "@/components/chat/HeldThreadMessage";
import { useSession } from "@/hooks/useSession";
import {
  fetchRoleThreadMessages,
  roleThreadsFromLabels,
  type RoleThreadDirection,
} from "@/lib/roleThreads";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

/** Label on an owner-started side chat naming its role-thread counterpart. */
export const THREAD_OF_LABEL_KEY = "thread.of";
/** Label on a side chat naming the session it was opened from. */
export const SIDE_CHAT_SOURCE_LABEL_KEY = "omnigent.side_chat.source_id";

const MESSAGE_STYLE: Record<RoleThreadDirection, string> = {
  in: "self-start",
  out: "self-end bg-muted",
  owner: "self-center border-primary/40 bg-primary/5",
  hub: "self-center border-dashed text-muted-foreground",
};

const SENDER_TEXT: Partial<Record<RoleThreadDirection, string>> = {
  owner: "You",
  hub: "Hub reminder",
};

/**
 * A role thread's messages, oldest first. Rendered at the top of the thread's
 * side chat, whose composer posts into the thread or sends an aside to the holder.
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
  const thread = roleThreadsFromLabels(holder?.labels).find(
    (row) => row.counterpart === counterpart,
  );
  const updatedAt = thread?.updatedAt;
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
      {thread?.held && (
        <HeldThreadMessage holderId={holderId} counterpart={counterpart} held={thread.held} />
      )}
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
            MESSAGE_STYLE[message.direction],
          )}
        >
          <p className="mb-1 text-xs text-muted-foreground">
            {[
              SENDER_TEXT[message.direction] ??
                (message.direction === "out" ? holderName : counterpart),
              ...(message.direction === "owner" ? ["to the thread"] : []),
              relativeTime(message.createdAt * 1000),
            ].join(" · ")}
          </p>
          <p className="break-words whitespace-pre-wrap">{message.text}</p>
        </div>
      ))}
    </section>
  );
}

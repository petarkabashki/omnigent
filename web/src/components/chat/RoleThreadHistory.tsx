import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SendIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useSession } from "@/hooks/useSession";
import {
  fetchRoleThreadMessages,
  onRoleThreadPostFocus,
  postRoleThreadMessage,
  roleThreadsFromLabels,
  type RoleThreadDirection,
  type RoleThreadPostTarget,
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
};

/**
 * A role thread's messages, oldest first, and a box for the owner to post into
 * it. Rendered at the top of the thread's side chat; the side chat's own
 * composer below stays an aside to the holder alone.
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
            MESSAGE_STYLE[message.direction],
          )}
        >
          <p className="mb-1 text-xs text-muted-foreground">
            {message.direction === "owner"
              ? "You"
              : message.direction === "out"
                ? holderName
                : counterpart}{" "}
            · {relativeTime(message.createdAt * 1000)}
          </p>
          <p className="break-words whitespace-pre-wrap">{message.text}</p>
        </div>
      ))}
      <ThreadPostBox holderId={holderId} holderName={holderName} counterpart={counterpart} />
    </section>
  );
}

/** The owner's composer: words go verbatim into the thread, to both roles or one side. */
function ThreadPostBox({
  holderId,
  holderName,
  counterpart,
}: {
  holderId: string;
  holderName: string;
  counterpart: string;
}) {
  const queryClient = useQueryClient();
  const input = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [to, setTo] = useState<RoleThreadPostTarget>("both");
  const [posting, setPosting] = useState(false);
  useEffect(() => onRoleThreadPostFocus(() => input.current?.focus()), []);
  const targets: [RoleThreadPostTarget, string][] = [
    ["both", "Both"],
    ["holder", holderName],
    ["counterpart", counterpart],
  ];

  const post = async () => {
    const message = text.trim();
    if (!message || posting) return;
    setPosting(true);
    try {
      await postRoleThreadMessage(holderId, counterpart, message, to);
      setText("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["role-thread-messages", holderId] }),
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't post to the thread.");
    } finally {
      setPosting(false);
    }
  };

  return (
    <form
      data-testid="role-thread-post"
      className="flex flex-col gap-1.5 pt-1"
      onSubmit={(event) => {
        event.preventDefault();
        void post();
      }}
    >
      <Textarea
        ref={input}
        aria-label="Post to the thread"
        placeholder="Post to the thread as yourself…"
        className="min-h-12 text-sm"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void post();
          }
        }}
      />
      <div className="flex items-center gap-1 text-xs text-muted-foreground">
        <span>To</span>
        <div role="radiogroup" aria-label="Post to" className="flex min-w-0 gap-0.5">
          {targets.map(([value, label]) => (
            <Button
              key={value}
              type="button"
              size="xs"
              role="radio"
              aria-checked={to === value}
              variant={to === value ? "secondary" : "ghost"}
              className="max-w-28 truncate"
              onClick={() => setTo(value)}
            >
              {label}
            </Button>
          ))}
        </div>
        <Button
          type="submit"
          size="xs"
          className="ml-auto"
          disabled={!text.trim() || posting}
          title="Post (Ctrl+Enter)"
        >
          {posting ? (
            <Loader2Icon aria-hidden className="animate-spin" />
          ) : (
            <SendIcon aria-hidden />
          )}
          Post
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        The box below is an aside to {holderName} only.
      </p>
    </form>
  );
}

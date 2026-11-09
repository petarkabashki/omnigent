import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { MessagesSquareIcon } from "lucide-react";
import { toast } from "sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { cn } from "@/lib/utils";
import { roleThreadStateText, roleThreadsFromLabels, type RoleThread } from "@/lib/roleThreads";
import { createSideChat } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { SIDEBAR_HOVER_HIGHLIGHT, SIDEBAR_ROW } from "./sidebarStyles";

/**
 * The threads of a role-holding session, listed under its sidebar row.
 * Opening one shows the thread in its side chat beside the holder, ready to reply.
 */
export function RoleThreadRows({ conversation }: { conversation: Conversation }) {
  const threads = roleThreadsFromLabels(conversation.labels);
  if (threads.length === 0) return null;
  return (
    <li data-testid="role-thread-rows">
      <ul
        aria-label={`Threads of ${conversation.title || "session"}`}
        className="flex flex-col gap-px pl-6"
      >
        {threads.map((thread) => (
          <RoleThreadRow key={thread.counterpart} holderId={conversation.id} thread={thread} />
        ))}
      </ul>
    </li>
  );
}

function RoleThreadRow({ holderId, thread }: { holderId: string; thread: RoleThread }) {
  const navigate = useNavigate();
  const [opening, setOpening] = useState(false);
  const urgent = thread.state === "blocked" || thread.state === "waiting_on_us";

  const open = async () => {
    if (opening) return;
    setOpening(true);
    navigate(`/c/${holderId}`);
    try {
      const { childSessionId } = await createSideChat(holderId, undefined, {
        thread: thread.counterpart,
      });
      useChatStore.getState().openSideChatWithDraft(childSessionId, "", holderId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't open the thread.");
    } finally {
      setOpening(false);
    }
  };

  return (
    <li>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            data-testid="role-thread-row"
            data-state-name={thread.state}
            onClick={() => void open()}
            aria-busy={opening}
            className={cn(
              SIDEBAR_ROW,
              SIDEBAR_HOVER_HIGHLIGHT,
              "flex w-full items-center text-left text-muted-foreground",
            )}
          >
            <MessagesSquareIcon aria-hidden className="size-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">{thread.counterpart}</span>
            {urgent && (
              <span className="shrink-0 text-xs text-amber-600 dark:text-amber-400">
                {roleThreadStateText(thread.state)}
              </span>
            )}
            {thread.unread > 0 && (
              <span
                aria-label={`${thread.unread} unread`}
                className="shrink-0 rounded-full bg-primary px-1.5 text-[10px] leading-4 font-medium text-primary-foreground"
              >
                {thread.unread}
              </span>
            )}
          </button>
        </TooltipTrigger>
        <TooltipContent side="right" className="max-w-72">
          <p className="font-medium">
            {thread.counterpart} · {roleThreadStateText(thread.state)} · {thread.messages}{" "}
            {thread.messages === 1 ? "message" : "messages"}
          </p>
          {thread.summary && <p className="mt-1">{thread.summary}</p>}
        </TooltipContent>
      </Tooltip>
    </li>
  );
}

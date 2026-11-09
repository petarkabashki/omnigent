import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRightIcon,
  CheckIcon,
  ClockIcon,
  MailOpenIcon,
  MessageSquareTextIcon,
  MessagesSquareIcon,
  RotateCcwIcon,
  SendIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import type { Conversation } from "@/hooks/useConversations";
import { useNamespaceRoles } from "@/hooks/useRoles";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { StateDot } from "@/components/StateDot";
import {
  requestRoleThreadPostFocus,
  roleHandlePath,
  roleThreadIsUrgent,
  roleThreadStateDescription,
  roleThreadStateLabels,
  roleThreadStateText,
  roleThreadStateTone,
  roleThreadsFromLabels,
  type RoleThread,
} from "@/lib/roleThreads";
import { createSideChat, updateSession } from "@/lib/sessionsApi";
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
  const urgent = roleThreadIsUrgent(thread.state);

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
      <HoverCard openDelay={400} closeDelay={150}>
        <HoverCardTrigger asChild>
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
            <span
              data-testid="role-thread-state"
              className="flex shrink-0 items-center gap-1 text-xs text-foreground"
            >
              <StateDot tone={roleThreadStateTone(thread.state)} />
              <span className={urgent ? undefined : "sr-only"}>
                {roleThreadStateText(thread.state)}
              </span>
            </span>
            {thread.unread > 0 && (
              <span
                aria-label={`${thread.unread} not yet handled by the role`}
                title="Messages the role hasn't handled yet"
                className="shrink-0 rounded-full bg-primary px-1.5 text-[10px] leading-4 font-medium text-primary-foreground"
              >
                {thread.unread}
              </span>
            )}
          </button>
        </HoverCardTrigger>
        <HoverCardContent side="right" align="start" className="w-60 p-2">
          <RoleThreadCard
            holderId={holderId}
            thread={thread}
            onOpen={() => void open()}
            onPost={() => {
              requestRoleThreadPostFocus();
              void open();
            }}
          />
        </HoverCardContent>
      </HoverCard>
    </li>
  );
}

/** Hover card for a thread row: state at a glance, then the actions on it. */
function RoleThreadCard({
  holderId,
  thread,
  onOpen,
  onPost,
}: {
  holderId: string;
  thread: RoleThread;
  onOpen: () => void;
  onPost: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [saving, setSaving] = useState(false);
  // Mounted only while the card is open, so the roles lookup runs on hover.
  const roles = useNamespaceRoles(roleHandlePath(thread.counterpart));
  const counterpartSessionId = roles.data?.find(
    (role) => role.handle === thread.counterpart && role.state === "held",
  )?.holderSessionId;
  const done = thread.state === "done";

  const setLabels = async (labels: Record<string, string>) => {
    setSaving(true);
    try {
      await updateSession(holderId, { labels, silent: true });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't update the thread.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="role-thread-card" className="flex flex-col gap-1.5">
      <p className="flex items-center gap-1.5 font-medium">
        <StateDot tone={roleThreadStateTone(thread.state)} />
        <span className="min-w-0 flex-1 truncate">{thread.counterpart}</span>
        <span
          className="shrink-0 text-xs font-normal text-muted-foreground"
          title={roleThreadStateDescription(thread.state) || undefined}
        >
          {roleThreadStateText(thread.state)}
        </span>
      </p>
      <p className="flex items-center gap-3 text-xs text-muted-foreground">
        <span className="flex items-center gap-1" title="Messages">
          <MessageSquareTextIcon aria-hidden className="size-3" />
          {thread.messages}
        </span>
        {thread.unread > 0 && (
          <span
            className="flex items-center gap-1 text-foreground"
            title="Messages the role hasn't handled yet; the role clears this when it notes the thread"
          >
            <span aria-hidden className="text-primary">
              ●
            </span>
            {thread.unread} unhandled
          </span>
        )}
        {thread.updatedAt > 0 && (
          <span className="flex items-center gap-1" title="Last activity">
            <ClockIcon aria-hidden className="size-3" />
            {relativeTime(thread.updatedAt * 1000)}
          </span>
        )}
      </p>
      {thread.summary && (
        <p className="line-clamp-2 text-xs" title={thread.summary}>
          {thread.summary}
        </p>
      )}
      <div className="flex flex-wrap gap-1 pt-0.5">
        <Button size="xs" variant="secondary" onClick={onOpen}>
          <MessagesSquareIcon aria-hidden /> Open
        </Button>
        <Button
          size="xs"
          variant="ghost"
          onClick={onPost}
          title="Write into the thread as yourself"
        >
          <SendIcon aria-hidden /> Post
        </Button>
        <Button
          size="xs"
          variant="ghost"
          disabled={!counterpartSessionId}
          title={counterpartSessionId ? undefined : `${thread.counterpart} isn't held right now`}
          onClick={() => counterpartSessionId && navigate(`/c/${counterpartSessionId}`)}
        >
          <ArrowUpRightIcon aria-hidden /> Go to
        </Button>
        {thread.unread > 0 && !done && (
          <Button
            size="xs"
            variant="ghost"
            disabled={saving}
            title="Clear the count for the role, as if it had noted the thread"
            onClick={() => void setLabels({ [`thread.${thread.key}.unread`]: "0" })}
          >
            <MailOpenIcon aria-hidden /> Handled
          </Button>
        )}
        <Button
          size="xs"
          variant="ghost"
          disabled={saving}
          onClick={() => void setLabels(roleThreadStateLabels(thread, done ? "open" : "done"))}
        >
          {done ? <RotateCcwIcon aria-hidden /> : <CheckIcon aria-hidden />}
          {done ? "Reopen" : "Done"}
        </Button>
      </div>
    </div>
  );
}

import { useQuery } from "@tanstack/react-query";
import { ArrowRightIcon, Loader2Icon, MessageSquarePlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { fetchRoleThreadMessages, roleThreadStateText, type RoleThread } from "@/lib/roleThreads";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

/**
 * One role thread's messages, both directions, oldest first. Replying happens
 * in the thread's side chat, which speaks for the role.
 */
export function RoleThreadDialog({
  open,
  onOpenChange,
  holderId,
  holderName,
  thread,
  onReply,
  onOpenSession,
  replying,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  holderId: string;
  holderName: string;
  thread: RoleThread;
  onReply: () => void;
  onOpenSession: () => void;
  replying: boolean;
}) {
  const messages = useQuery({
    queryKey: ["role-thread-messages", holderId, thread.counterpart, thread.updatedAt],
    queryFn: () => fetchRoleThreadMessages(holderId, thread.counterpart),
    enabled: open,
    retry: 1,
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="role-thread-dialog" className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {holderName} ↔ {thread.counterpart}
          </DialogTitle>
          <DialogDescription>{roleThreadStateText(thread.state)}</DialogDescription>
        </DialogHeader>
        <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto">
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
                {message.direction === "out" ? holderName : thread.counterpart} ·{" "}
                {relativeTime(message.createdAt * 1000)}
              </p>
              <p className="break-words whitespace-pre-wrap">{message.text}</p>
            </div>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onOpenSession}>
            Open {holderName}
            <ArrowRightIcon aria-hidden className="size-3.5" />
          </Button>
          <Button size="sm" onClick={onReply} disabled={replying}>
            <MessageSquarePlusIcon aria-hidden className="size-3.5" />
            Reply in side chat
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

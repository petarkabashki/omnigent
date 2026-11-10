import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { relativeTime } from "@/lib/relativeTime";
import {
  resolveHeldThreadMessage,
  type HeldThreadAction,
  type HeldThreadMessage as HeldMessage,
} from "@/lib/roleThreads";

const ACTIONS: { action: HeldThreadAction; label: string; title: string; done: string }[] = [
  {
    action: "release",
    label: "Release",
    title: "Deliver this one message; the loop counters stay as they are",
    done: "Delivered",
  },
  {
    action: "allow",
    label: "Allow more",
    title: "Deliver this message and reset the pair's loop counters so they can keep talking",
    done: "Delivered; counters reset",
  },
  {
    action: "discard",
    label: "Discard",
    title: "Drop this message; the sender is told it was dropped",
    done: "Discarded; sender told",
  },
];

/** Amber notice for a message the loop guard is holding, with the owner's three choices. */
export function HeldThreadMessage({
  holderId,
  counterpart,
  held,
}: {
  holderId: string;
  counterpart: string;
  held: HeldMessage;
}) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const resolve = async (action: HeldThreadAction, done: string) => {
    if (pending) return;
    setPending(true);
    try {
      await resolveHeldThreadMessage(holderId, counterpart, action);
      toast.success(done);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
        queryClient.invalidateQueries({ queryKey: ["session", holderId] }),
        queryClient.invalidateQueries({ queryKey: ["role-thread-messages", holderId] }),
      ]);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't resolve the held message.");
    } finally {
      setPending(false);
    }
  };

  return (
    <div
      data-testid="held-thread-message"
      role="status"
      className="flex flex-col gap-1 rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-xs"
    >
      <p className="font-medium">
        Held by loop guard
        {held.at ? ` · ${relativeTime(held.at * 1000)}` : ""}
      </p>
      <p
        data-testid="held-thread-text"
        className={`break-words whitespace-pre-wrap ${expanded ? "" : "line-clamp-4"}`}
      >
        {held.text}
      </p>
      {held.text.length > 160 && (
        <button
          type="button"
          className="self-start text-muted-foreground underline"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
      <p className="text-muted-foreground">
        Counts reset after 10 quiet minutes or any message from you.
      </p>
      <div className="flex flex-wrap gap-1 pt-0.5">
        {ACTIONS.map(({ action, label, title, done }) => (
          <Button
            key={action}
            type="button"
            size="xs"
            variant={action === "release" ? "secondary" : "ghost"}
            disabled={pending}
            title={title}
            onClick={() => void resolve(action, done)}
          >
            {label}
          </Button>
        ))}
      </div>
    </div>
  );
}

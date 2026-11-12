import {
  BellRingIcon,
  BotIcon,
  ClockIcon,
  HourglassIcon,
  MessageSquareIcon,
  ServerOffIcon,
  ShieldQuestionIcon,
  SquareIcon,
  TerminalIcon,
  UserXIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { RoleThread } from "@/lib/roleThreads";
import { cn } from "@/lib/utils";
import { formatWaitAge, type WaitingItem, type WaitingKind } from "@/lib/waitingApi";

const KIND_ICON: Record<WaitingKind, LucideIcon> = {
  tool_call: WrenchIcon,
  async_task: HourglassIcon,
  background_shell: TerminalIcon,
  subagent: BotIcon,
  approval: ShieldQuestionIcon,
  queued_message: MessageSquareIcon,
  thread: BellRingIcon,
  runner_offline: ServerOffIcon,
  waiting_holder: UserXIcon,
};

/** The thread a thread item stands for, enough for the nudge text. */
export function threadFromWaitingItem(item: WaitingItem): RoleThread {
  return {
    key: "",
    counterpart: item.ref ?? "",
    state: "waiting_on_us",
    unread: 1,
    messages: 0,
    summary: "",
    updatedAt: item.since ?? 0,
  };
}

/** Session a sub-agent item opens: its child session when `ref` names one. */
export function waitingItemTarget(item: WaitingItem): string {
  return item.kind === "subagent" && item.ref ? item.ref : item.session_id;
}

export interface WaitingItemRowProps {
  item: WaitingItem;
  now?: number;
  onOpen: (sessionId: string) => void;
  onNudge: (item: WaitingItem) => void;
  onStop: (sessionId: string) => void;
}

/** One waiting item with its age, host and Open / Nudge / Stop actions. */
export function WaitingItemRow({ item, now, onOpen, onNudge, onStop }: WaitingItemRowProps) {
  const Icon = item.stale ? ClockIcon : KIND_ICON[item.kind];
  const age = item.since != null ? formatWaitAge(item.since, now) : null;
  return (
    <li
      data-testid="waiting-item"
      data-kind={item.kind}
      data-stale={item.stale ? "true" : "false"}
      className="flex items-start gap-2 py-1.5"
    >
      <Icon
        aria-hidden
        className={cn(
          "mt-0.5 size-3.5 shrink-0",
          item.stale ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate text-ui font-medium" title={item.title}>
          {item.title}
        </div>
        <div
          className={cn(
            "flex flex-wrap gap-x-1.5 text-xs",
            item.stale ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
          )}
        >
          {item.detail && <span>{item.detail}</span>}
          {age && <span data-testid="waiting-item-age">{age}</span>}
          {item.where && <span>on {item.where}</span>}
        </div>
      </div>
      <div className="flex shrink-0 gap-1">
        {item.actions.includes("open") && (
          <Button size="xs" variant="ghost" onClick={() => onOpen(waitingItemTarget(item))}>
            Open
          </Button>
        )}
        {item.actions.includes("nudge") && (
          <Button size="xs" variant="ghost" onClick={() => onNudge(item)}>
            Nudge
          </Button>
        )}
        {item.actions.includes("stop") && (
          <Button
            size="xs"
            variant="ghost"
            aria-label="Stop the current turn"
            title="Stop the current turn; the session stays open"
            onClick={() => onStop(item.session_id)}
          >
            <SquareIcon className="size-3" />
            Stop
          </Button>
        )}
      </div>
    </li>
  );
}

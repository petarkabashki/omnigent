// Sidebar status indicator. Approval surfaces as a "Needs you" tag so
// it reads at a glance; other states stay compact. Verbose copy
// (incl. the approval count) lives in the tooltip.

import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionState } from "@/hooks/useSessionState";
import { TERMINAL_PROMPT_BLOCKED_ON } from "@/lib/events";
import { cn } from "@/lib/utils";
import type { ReactElement } from "react";
import { CircleAlertIcon } from "lucide-react";

export interface SessionStateBadgeProps {
  state: SessionState;
  /** Replaces the default tooltip, e.g. "Running 12 min". */
  tooltip?: string;
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => ReactElement;
}

function awaitingTooltip(count: number, terminalReason: string | undefined): string {
  if (terminalReason === TERMINAL_PROMPT_BLOCKED_ON) {
    return "May need your input in the agent's terminal";
  }
  if (terminalReason) return `Waiting for you in the agent's terminal (${terminalReason})`;
  return count === 1 ? "1 approval prompt waiting" : `${count} approval prompts waiting`;
}

function describe(state: SessionState): Visual {
  switch (state.kind) {
    case "awaiting": {
      const reason = state.reason ?? awaitingTooltip(state.count, state.terminalReason);
      // The runner infers this reason from the terminal's screen, so stay tentative.
      const tentative = state.terminalReason === TERMINAL_PROMPT_BLOCKED_ON;
      const label = tentative
        ? "May need input"
        : state.terminalReason && !state.reason
          ? "Needs response"
          : "Needs you";
      return {
        kind: state.kind,
        ariaLabel: reason,
        tooltip: tentative ? reason : `Needs you: ${reason}`,
        render: () => (
          <Badge className="border-transparent bg-brand-accent/15 text-brand-accent">{label}</Badge>
        ),
      };
    }
    case "running": {
      const tooltip = state.background
        ? "Working in the background: the turn ended but its work continues."
        : "Running: the agent is working on a turn.";
      return {
        kind: state.kind,
        ariaLabel: state.background ? "Working in the background" : "Session running",
        tooltip,
        render: () => <RunningDot className="size-3" />,
      };
    }
    case "starting":
      // Same spinner as running — the session is coming up, not yet working.
      return {
        kind: state.kind,
        ariaLabel: "Session starting up",
        tooltip: "Session starting up",
        render: () => <RunningDot className="size-3" />,
      };
    case "error":
      return {
        kind: state.kind,
        ariaLabel: "Latest message is an error",
        tooltip: "Failed: the last turn ended with an error.",
        render: () => (
          <CircleAlertIcon aria-hidden className="size-3.5 shrink-0 text-destructive" />
        ),
      };
    case "disconnected":
      return {
        kind: state.kind,
        ariaLabel: "Host disconnected",
        tooltip: "Offline: the session's host isn't connected.",
        render: () => (
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full border border-muted-foreground"
          />
        ),
      };
    case "unseen":
      // Solid brand-pink dot — distinguished from the running indicator,
      // which is a grey spinner.
      return {
        kind: state.kind,
        ariaLabel: "New messages",
        tooltip: "New messages",
        render: () => <Dot tone="bg-brand-accent" />,
      };
  }
}

function Dot({ tone }: { tone: string }) {
  return <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", tone)} />;
}

export function SessionStateBadge({ state, tooltip }: SessionStateBadgeProps) {
  const visual = describe(state);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="session-state-badge"
          data-state={visual.kind}
          role="img"
          aria-label={visual.ariaLabel}
          className="inline-flex h-5 shrink-0 items-center justify-center"
        >
          {visual.render()}
        </span>
      </TooltipTrigger>
      {/* Opens left: the badge sits at the right edge of the narrow
          sidebar, so a right-opening tooltip would overflow the panel. */}
      <TooltipContent side="left">{tooltip ?? visual.tooltip}</TooltipContent>
    </Tooltip>
  );
}

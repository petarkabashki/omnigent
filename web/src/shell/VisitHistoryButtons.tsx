import { ArrowLeftIcon, ArrowRightIcon, CheckIcon, HistoryIcon } from "lucide-react";
import { ALT_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useVisitHistory } from "@/hooks/useVisitHistory";
import { cn } from "@/lib/utils";

const MAX_LISTED_VISITS = 50;
const BUTTON_CLASS = "border-none text-muted-foreground";

/** Back, forward and the tab's visit list. Hidden until the tab has a record. */
export function VisitHistoryButtons({ className }: { className?: string }) {
  const { entries, cursor, canGoBack, canGoForward, go } = useVisitHistory();
  if (cursor < 0) return null;

  const visits = entries
    .flatMap((entry, index) => (entry ? [{ entry, index }] : []))
    .slice(-MAX_LISTED_VISITS)
    .reverse();

  return (
    <div className={cn("flex items-center", className)} data-testid="visit-history-buttons">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Go back"
            data-testid="history-back"
            disabled={!canGoBack}
            onClick={() => go(-1)}
            className={BUTTON_CLASS}
          >
            <ArrowLeftIcon className="size-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent shortcut={[ALT_KEY, "←"]}>Back</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Go forward"
            data-testid="history-forward"
            disabled={!canGoForward}
            onClick={() => go(1)}
            className={BUTTON_CLASS}
          >
            <ArrowRightIcon className="size-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent shortcut={[ALT_KEY, "→"]}>Forward</TooltipContent>
      </Tooltip>
      <DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Recent visits"
                data-testid="history-list"
                className={BUTTON_CLASS}
              >
                <HistoryIcon className="size-4" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>Recent visits</TooltipContent>
        </Tooltip>
        <DropdownMenuContent align="start" className="max-h-80 w-64 overflow-y-auto">
          {visits.map(({ entry, index }) => (
            <DropdownMenuItem
              key={`${index}-${entry.key}`}
              disabled={index === cursor}
              onSelect={() => go(index - cursor)}
            >
              <span className="min-w-0 flex-1 truncate">{entry.label}</span>
              {index === cursor && <CheckIcon className="size-4 shrink-0" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

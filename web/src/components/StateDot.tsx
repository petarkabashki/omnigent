import { cn } from "@/lib/utils";

/** Colour family for a state mark; the word beside it carries the meaning. */
export type StateTone = "amber" | "blue" | "neutral" | "red" | "green" | "yellow";

const TONE_CLASS: Record<StateTone, string> = {
  amber: "bg-amber-500",
  blue: "bg-blue-500",
  neutral: "bg-muted-foreground/40",
  red: "bg-red-500",
  green: "bg-emerald-500",
  yellow: "bg-yellow-400",
};

/** A small coloured dot that sits before a state word; hidden from screen readers. */
export function StateDot({ tone, className }: { tone: StateTone; className?: string }) {
  return (
    <span
      aria-hidden
      data-tone={tone}
      className={cn("inline-block size-2 shrink-0 rounded-full", TONE_CLASS[tone], className)}
    />
  );
}

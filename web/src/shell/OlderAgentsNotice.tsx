import { Button } from "@/components/ui/button";

/**
 * Says the loaded sub-agent list leaves out older agents, with a Load more
 * button and the last failure.
 */
export function OlderAgentsNotice({
  shown,
  loading,
  error,
  onLoadMore,
}: {
  shown: number;
  loading: boolean;
  error: string | null;
  onLoadMore: () => Promise<void>;
}) {
  return (
    <div
      data-testid="older-agents-notice"
      className="flex flex-wrap items-center gap-2 py-1 text-sm text-muted-foreground"
    >
      <span>Showing {shown} — older agents not shown.</span>
      {error && (
        <span role="alert" className="text-destructive">
          {error}
        </span>
      )}
      <Button
        variant="ghost"
        size="xs"
        disabled={loading}
        data-testid="older-agents-load-more"
        onClick={() => void onLoadMore()}
      >
        {loading ? "Loading…" : "Load more"}
      </Button>
    </div>
  );
}

import { useSession } from "@/hooks/useSession";
import { isExternalTaskLabels } from "@/lib/sessionCapabilities";
import { StateDot, type StateTone } from "./StateDot";

const FIELDS: [label: string, key: string][] = [
  ["State", "omnigent.external.state"],
  ["Exit code", "omnigent.external.exit_code"],
  ["Result", "omnigent.external.detail"],
  ["Host", "omnigent.external.host_id"],
  ["Unit", "omnigent.external.unit"],
];

const EXTERNAL_STATE_TONE: Record<string, StateTone> = {
  queued: "neutral",
  running: "green",
  done: "green",
  failed: "red",
  stopped: "neutral",
};

/** What the task's state means, with its host, exit code and detail where set. */
function externalStateDescription(labels: Record<string, string>): string {
  const state = labels["omnigent.external.state"];
  const host = labels["omnigent.external.host_id"];
  const unit = labels["omnigent.external.unit"];
  const code = labels["omnigent.external.exit_code"];
  const detail = labels["omnigent.external.detail"];
  switch (state) {
    case "queued":
      return "Queued: waiting for a slot on a host.";
    case "running":
      return `Running${host ? ` on ${host}` : ""}${unit ? ` as unit ${unit}` : ""}.`;
    case "done":
      return `Done: exited ${code ?? "0"}.`;
    case "failed":
      return `Failed: ${[code && `exit ${code}`, detail].filter(Boolean).join(", ") || "see the result"}.`;
    case "stopped":
      return "Stopped on purpose.";
    default:
      return state ?? "";
  }
}

/** Read-only status panel for rows that mirror work outside Omnigent. */
export function ExternalTaskPanel({ conversationId }: { conversationId: string | null }) {
  const { session } = useSession(conversationId);
  const labels = session?.labels;
  if (!isExternalTaskLabels(labels)) return null;
  return (
    <dl
      data-testid="external-task-panel"
      className="mx-auto mb-2 grid w-full max-w-3xl grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md border p-3 text-sm"
    >
      {FIELDS.map(([label, key]) => {
        const value = labels?.[key];
        if (!value) return null;
        return (
          <div key={key} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd
              className="flex items-center gap-1.5 font-mono break-all"
              title={
                key === "omnigent.external.state" && labels
                  ? externalStateDescription(labels)
                  : undefined
              }
            >
              {key === "omnigent.external.state" && (
                <StateDot tone={EXTERNAL_STATE_TONE[value] ?? "neutral"} />
              )}
              {value}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

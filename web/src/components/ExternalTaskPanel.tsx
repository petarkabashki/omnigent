import { useSession } from "@/hooks/useSession";
import { isExternalTaskLabels } from "@/lib/sessionCapabilities";

const FIELDS: [label: string, key: string][] = [
  ["State", "omnigent.external.state"],
  ["Exit code", "omnigent.external.exit_code"],
  ["Result", "omnigent.external.detail"],
  ["Host", "omnigent.external.host_id"],
  ["Unit", "omnigent.external.unit"],
];

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
            <dd className="font-mono break-all">{value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

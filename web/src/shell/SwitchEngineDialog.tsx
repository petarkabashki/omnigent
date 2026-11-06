import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAvailableAgents, prefetchAvailableAgentDetails } from "@/hooks/useAvailableAgents";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { partitionAgentsByKind } from "@/lib/agentGrouping";
import { harnessUnavailableReasonOnHost } from "@/lib/harnessSetup";
import { randomUUID } from "@/lib/randomUUID";
import { switchSessionEngine, type SwitchEngineOverrides } from "@/lib/sessionsApi";
import { SWITCH_REFUSAL_COPY, switchRefusalOf, type SwitchRefusal } from "@/lib/engineSwitch";
import { AgentTargetSelect } from "./AgentTargetSelect";

/**
 * Agents the session can move to: a known harness other than the current one
 * that the bound host reports ready. A host that reports nothing keeps every
 * harness (readiness unknown), and the server still enforces the final gates.
 */
export function switchEngineTargets(
  agents: readonly AvailableAgent[],
  currentHarness: string | null | undefined,
  host: Host | null | undefined,
): AvailableAgent[] {
  return agents.filter(
    (agent) =>
      agent.harness != null &&
      agent.harness !== currentHarness &&
      harnessUnavailableReasonOnHost(agent.harness, host) === null,
  );
}

/**
 * Dialog that moves the open session onto another engine in place. The target
 * picker is the fork dialog's agent select; the request is a session reset
 * carrying the chosen agent.
 */
export function SwitchEngineDialog({
  sessionId,
  currentHarness,
  hostId,
  open,
  onOpenChange,
}: {
  sessionId: string;
  currentHarness: string | null | undefined;
  hostId: string | null | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const { data: agents } = useAvailableAgents({ enabled: open });
  const { data: hosts } = useHosts({ enabled: open && !!hostId });
  const host = hostId ? hosts?.find((candidate) => candidate.host_id === hostId) : undefined;
  const [agentId, setAgentId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Refusal awaiting the user's confirmation, and the overrides already confirmed.
  const [refusal, setRefusal] = useState<SwitchRefusal | null>(null);
  const [overrides, setOverrides] = useState<SwitchEngineOverrides>({});
  // One key per chosen agent: a retry of the same choice is safe, a new choice is not.
  const attemptKey = useRef<{ agentId: string; key: string } | null>(null);

  // A reopened dialog starts clean: no remembered confirmations or key.
  useEffect(() => {
    if (open) return;
    setRefusal(null);
    setOverrides({});
    attemptKey.current = null;
  }, [open]);

  // Catalog rows can report harness=null until their details load.
  useEffect(() => {
    for (const agent of agents ?? []) void prefetchAvailableAgentDetails(agent, queryClient);
  }, [agents, queryClient]);

  const targets = useMemo(
    () => switchEngineTargets(agents ?? [], currentHarness, host),
    [agents, currentHarness, host],
  );
  const { builtins, customs } = useMemo(() => partitionAgentsByKind(targets), [targets]);
  const selected = targets.find((agent) => agent.id === agentId);

  async function submit(confirmed: SwitchEngineOverrides = overrides) {
    if (!selected || submitting) return;
    if (attemptKey.current?.agentId !== selected.id) {
      attemptKey.current = { agentId: selected.id, key: randomUUID() };
    }
    setSubmitting(true);
    setError(null);
    setRefusal(null);
    setOverrides(confirmed);
    try {
      await switchSessionEngine(
        sessionId,
        { agentId: selected.id },
        attemptKey.current.key,
        confirmed,
      );
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      onOpenChange(false);
    } catch (err) {
      const needed = switchRefusalOf(err);
      if (needed) {
        setRefusal(needed);
        return;
      }
      setError(err instanceof Error ? err.message : "Couldn't switch the engine.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="switch-engine-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Switch engine</DialogTitle>
          <DialogDescription>
            This session keeps its id and handle. The new engine starts from a handoff note, not the
            old engine&apos;s memory, so it may not recall details that were never written down.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="switch-engine-agent"
            className="text-sm font-medium text-muted-foreground"
          >
            New engine
          </label>
          {targets.length === 0 ? (
            <p data-testid="switch-engine-empty" className="text-sm text-muted-foreground">
              No other engine is installed on this session&apos;s host.
            </p>
          ) : (
            <AgentTargetSelect
              id="switch-engine-agent"
              testIdPrefix="switch-engine-agent"
              componentId="switch_engine.agent"
              value={agentId}
              onValueChange={(next) => {
                setAgentId(next);
                setError(null);
                setRefusal(null);
                setOverrides({});
              }}
              builtins={builtins}
              customs={customs}
              selectedContent={selected?.display_name}
              placeholder="Choose an engine"
              disabled={submitting}
            />
          )}
        </div>
        {refusal && (
          <div
            role="alert"
            data-testid="switch-engine-confirm"
            className="flex flex-col gap-2 rounded-md border border-destructive/40 p-3 text-sm"
          >
            <p>{SWITCH_REFUSAL_COPY[refusal].warning}</p>
            <Button
              variant="destructive"
              size="sm"
              className="self-start"
              data-testid="switch-engine-confirm-submit"
              disabled={submitting}
              onClick={() =>
                void submit({
                  ...overrides,
                  ...(refusal === "pending" && { dropPending: true }),
                  ...(refusal === "running" && { stopRunning: true }),
                  ...(refusal === "work" && { acknowledgeRunningWork: true }),
                })
              }
            >
              {SWITCH_REFUSAL_COPY[refusal].action}
            </Button>
          </div>
        )}
        {error && (
          <p role="alert" data-testid="switch-engine-error" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="switch-engine-submit"
            disabled={!selected || submitting || refusal !== null}
            onClick={() => void submit()}
          >
            {submitting ? "Switching…" : "Switch engine"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

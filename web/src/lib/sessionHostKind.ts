// Classifies where a session runs (this device, another host, a sandbox) for the sidebar.

import type { Conversation } from "@/hooks/useConversations";
import type { Host } from "@/hooks/useHosts";

export type SessionHostKind = "local" | "remote" | "sandbox";

export interface SessionHostInfo {
  kind: SessionHostKind;
  /** Host name for remote hosts; the host id while the host is unresolved. */
  name: string;
  sandboxProvider: string | null;
  /** True only when the host is known and reported offline. */
  offline: boolean;
}

/** `thisHostId` is the host id of the device running this UI, or null when unknown. */
export function classifySessionHost(
  conversation: Pick<Conversation, "host_id">,
  hostsById: ReadonlyMap<string, Host>,
  thisHostId: string | null,
): SessionHostInfo {
  const hostId = conversation.host_id;
  if (!hostId) return { kind: "local", name: "", sandboxProvider: null, offline: false };
  const host = hostsById.get(hostId);
  const offline = host?.status === "offline";
  if (host?.sandbox_provider) {
    return { kind: "sandbox", name: host.name, sandboxProvider: host.sandbox_provider, offline };
  }
  const kind = hostId === thisHostId ? "local" : "remote";
  return { kind, name: host?.name ?? hostId, sandboxProvider: null, offline };
}

const MACHINE_TAG_MAX = 4;

/**
 * At most four letters or digits from the first word, for a row tag such as
 * `[hub]`. `hpz4` stays `hpz4`. `optiplex` becomes `opti`. `gpu26` becomes `gpu2`.
 */
export function machineRowTag(name: string): string | null {
  const word = name
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .find((part) => part.length > 0);
  if (word === undefined) return null;
  return word.slice(0, MACHINE_TAG_MAX);
}

/**
 * Short tag for where the session runs. Uses the host's own name, including
 * this machine, so a host named hub reads `[hub]`. A sandbox uses its provider.
 * An unbound local session has no name and no tag.
 */
export function sessionMachineTag(info: SessionHostInfo): string | null {
  if (info.kind === "sandbox") return machineRowTag(info.sandboxProvider ?? info.name);
  return machineRowTag(info.name);
}

const KIND_RANK: Record<SessionHostKind, number> = { local: 0, remote: 1, sandbox: 2 };

/** Group titles ordered: this device, then hosts alphabetically, sandboxes last. */
export function orderHostGroups(
  groups: readonly { label: string; kind: SessionHostKind }[],
): string[] {
  const kindByLabel = new Map<string, SessionHostKind>();
  for (const { label, kind } of groups) {
    const seen = kindByLabel.get(label);
    if (seen === undefined || KIND_RANK[kind] < KIND_RANK[seen]) kindByLabel.set(label, kind);
  }
  return [...kindByLabel.entries()]
    .sort(([a, ka], [b, kb]) => KIND_RANK[ka] - KIND_RANK[kb] || a.localeCompare(b))
    .map(([label]) => label);
}

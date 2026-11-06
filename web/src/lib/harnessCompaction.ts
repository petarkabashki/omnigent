import { useQuery } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";

/** How `/compact` runs for a harness; mirrors the server's `CompactionMode`. */
export type CompactionMode = "native" | "reset_replay" | "none";

interface HarnessCatalogRow {
  id?: unknown;
  capabilities?: { compaction_mode?: unknown } | null;
}

function isCompactionMode(value: unknown): value is CompactionMode {
  return value === "native" || value === "reset_replay" || value === "none";
}

/**
 * Read each harness's declared compaction mode from a `/v1/harnesses` body.
 * Older servers omit the field, so their harnesses are simply absent here.
 */
export function compactionModesFromCatalog(body: {
  data?: readonly HarnessCatalogRow[];
}): Record<string, CompactionMode> {
  const modes: Record<string, CompactionMode> = {};
  for (const row of body.data ?? []) {
    const mode = row.capabilities?.compaction_mode;
    if (typeof row.id !== "string" || !isCompactionMode(mode)) continue;
    modes[row.id] = mode;
    // The base `acp` harness has no row of its own, only its configured agents.
    if (row.id.startsWith("acp:")) modes.acp = mode;
  }
  return modes;
}

/** Look up a harness in the catalog; every `acp:<slug>` shares the `acp` mode. */
export function compactionModeFor(
  modes: Record<string, CompactionMode>,
  harness: string | null | undefined,
): CompactionMode | undefined {
  if (!harness) return undefined;
  return modes[harness.startsWith("acp:") ? "acp" : harness];
}

async function fetchCompactionModes(): Promise<Record<string, CompactionMode>> {
  const res = await authenticatedFetch("/v1/harnesses");
  if (!res.ok) return {};
  return compactionModesFromCatalog((await res.json()) as { data?: HarnessCatalogRow[] });
}

/**
 * The compaction mode for `harness`, or `undefined` while loading, on an older
 * server, or for a harness the catalog does not list.
 */
export function useHarnessCompactionMode(
  harness: string | null | undefined,
): CompactionMode | undefined {
  const { data } = useQuery({
    queryKey: ["harness-compaction-modes"],
    queryFn: fetchCompactionModes,
    staleTime: 30_000,
    enabled: Boolean(harness),
  });
  return data ? compactionModeFor(data, harness) : undefined;
}

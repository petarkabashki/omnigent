// Role lanes: top-level sessions that work for a role, nested under its holder's sidebar row.

import { sessionRoleHandle } from "@/lib/rolesApi";

/** Names the role a lane works for, as its handle: `<project>/<Alias>`. */
export const ROLE_LANE_LABEL_KEY = "omnigent.role_lane";
/** The lane's own name within its role, a kebab-case slug such as `c30`. */
export const LANE_NAME_LABEL_KEY = "omnigent.lane_name";

// `*` is reserved for addressing every lane, so it is never a lane name.
const LANE_NAME = /^[a-z0-9-]{1,32}$/;

/** A lane's slug name, or null when it has none or it isn't a valid slug. */
export function roleLaneName(labels: Record<string, string> | null | undefined): string | null {
  const name = labels?.[LANE_NAME_LABEL_KEY]?.trim() ?? "";
  return LANE_NAME.test(name) ? name : null;
}

interface LaneCandidate {
  id: string;
  project_id?: string | null;
  labels?: Record<string, string> | null;
}

export interface RoleLaneGroups<T> {
  /** Lanes under each role row, keyed by the holder's session id, in input order. */
  lanesByHolderId: Map<string, T[]>;
  /** Every row shown as a lane, so the top-level list skips it. */
  laneIds: Set<string>;
}

/** The role handle a lane names, or null for a row that isn't a lane. */
export function roleLaneHandle(labels: Record<string, string> | null | undefined): string | null {
  return labels?.[ROLE_LANE_LABEL_KEY]?.trim() || null;
}

/**
 * Pairs lanes with the role rows present in `rows`. A lane whose role has no row here
 * (vacant, missing, or filtered out) is left out of the result and stays top level.
 * A row that holds a role is never a lane, so nesting is one level deep.
 */
export function groupRoleLanes<T extends LaneCandidate>(
  rows: readonly T[],
  projectNamesById: ReadonlyMap<string, string>,
): RoleLaneGroups<T> {
  const holderByHandle = new Map<string, string>();
  for (const row of rows) {
    const projectPath = row.project_id != null ? projectNamesById.get(row.project_id) : null;
    const handle = sessionRoleHandle(row.labels, projectPath, row.project_id != null);
    if (handle) holderByHandle.set(handle.toLowerCase(), row.id);
  }
  const holderIds = new Set(holderByHandle.values());
  const lanesByHolderId = new Map<string, T[]>();
  const laneIds = new Set<string>();
  for (const row of rows) {
    const handle = roleLaneHandle(row.labels);
    if (!handle || holderIds.has(row.id)) continue;
    const holderId = holderByHandle.get(handle.toLowerCase());
    if (!holderId) continue;
    const lanes = lanesByHolderId.get(holderId) ?? [];
    lanes.push(row);
    lanesByHolderId.set(holderId, lanes);
    laneIds.add(row.id);
  }
  return { lanesByHolderId, laneIds };
}

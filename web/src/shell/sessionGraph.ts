import type { Conversation } from "@/hooks/useConversations";
import { sessionStatus, type AgentActivity } from "./subagentStatus";
import { layoutTree, NODE_WIDTH, type TreeNode } from "./subagentGraphLayout";

export const FLEET_DISPATCHER_LABEL_KEY = "ag-fleet.dispatched_by";
export const FLEET_HOST_LABEL_KEY = "ag-fleet.host";
export const FLEET_TASK_LABEL_KEY = "ag-fleet.task";
const CREATED_BY_LABEL_KEY = "omnigent.created_by_session_id";
const FORK_SOURCE_LABEL_KEY = "omnigent.fork.source_id";

export type SessionLinkKind = "dispatched" | "created" | "forked";

const LINK_LABELS: [string, SessionLinkKind][] = [
  [FLEET_DISPATCHER_LABEL_KEY, "dispatched"],
  [CREATED_BY_LABEL_KEY, "created"],
  [FORK_SOURCE_LABEL_KEY, "forked"],
];

const SECTION_WIDTH = 620;
const SECTION_COLUMNS = 4;
const TREE_GAP = 40;
const ROW_GAP = 40;
const SECTION_GAP = 70;
const HEADING_OFFSET = 34;
const TREE_LEVEL_HEIGHT = 90;
const NO_PROJECT = "No project";

/** The session this one came from, when that session is also on the graph. */
export function sessionLink(
  session: Conversation,
  sessionIds: ReadonlySet<string>,
): { sourceId: string; kind: SessionLinkKind } | null {
  for (const [labelKey, kind] of LINK_LABELS) {
    const sourceId = session.labels?.[labelKey];
    if (sourceId && sourceId !== session.id && sessionIds.has(sourceId)) return { sourceId, kind };
  }
  return null;
}

/** Where a session runs: the machine, then the workspace folder. */
export function sessionLocation(
  session: Conversation,
  hostNames: ReadonlyMap<string, string>,
): string {
  const host =
    session.labels?.[FLEET_HOST_LABEL_KEY] ??
    (session.host_id ? hostNames.get(session.host_id) : undefined);
  const folder = session.workspace?.split("/").filter(Boolean).pop();
  return [host, folder].filter(Boolean).join(" · ") || "No machine";
}

function sessionActivity(session: Conversation): { activity: AgentActivity; label: string } {
  if ((session.pending_elicitations_count ?? 0) > 0) {
    return { activity: "awaiting", label: "Needs response" };
  }
  const unbound = !session.host_id && !session.runner_id;
  if (session.labels?.[FLEET_TASK_LABEL_KEY] && unbound) {
    return { activity: "other", label: "Remote task" };
  }
  return sessionStatus(session.status);
}

interface GraphOptions {
  hostNames: ReadonlyMap<string, string>;
  projectNames: ReadonlyMap<string, string>;
  activeId: string | null;
}

function toTree(
  session: Conversation,
  childrenOf: ReadonlyMap<string, Conversation[]>,
  hostNames: ReadonlyMap<string, string>,
  visited: Set<string>,
): TreeNode {
  visited.add(session.id);
  const status = sessionActivity(session);
  return {
    id: session.id,
    label: session.title || session.id,
    activity: status.activity,
    statusLabel: status.label,
    preview: sessionLocation(session, hostNames),
    children: (childrenOf.get(session.id) ?? [])
      .filter((child) => !visited.has(child.id))
      .map((child) => toTree(child, childrenOf, hostNames, visited)),
  };
}

/**
 * Lay out sessions as provenance trees, one section per project. A tree sits in
 * its root's project, so a session started from another project stays beside
 * whoever started it.
 */
export function buildSessionGraph(sessions: Conversation[], options: GraphOptions) {
  const { hostNames, projectNames, activeId } = options;
  const sessionIds = new Set(sessions.map((session) => session.id));
  const childrenOf = new Map<string, Conversation[]>();
  const links = new Map<string, SessionLinkKind>();
  const roots: Conversation[] = [];
  for (const session of sessions) {
    const link = sessionLink(session, sessionIds);
    if (!link) {
      roots.push(session);
      continue;
    }
    childrenOf.set(link.sourceId, [...(childrenOf.get(link.sourceId) ?? []), session]);
    links.set(session.id, link.kind);
  }

  // Sessions that only name each other have no root; promote one so none vanish.
  const reached = new Set<string>();
  const markReached = (session: Conversation) => {
    if (reached.has(session.id)) return;
    reached.add(session.id);
    (childrenOf.get(session.id) ?? []).forEach(markReached);
  };
  roots.forEach(markReached);
  for (const session of sessions) {
    if (reached.has(session.id)) continue;
    roots.push(session);
    markReached(session);
  }

  const sections = new Map<string, Conversation[]>();
  for (const root of roots) {
    const name = (root.project_id && projectNames.get(root.project_id)) || NO_PROJECT;
    sections.set(name, [...(sections.get(name) ?? []), root]);
  }

  type Layout = ReturnType<typeof layoutTree>;
  const nodes: Layout["nodes"] = [];
  const edges: (Layout["edges"][number] & { label?: string })[] = [];
  const headings: {
    id: string;
    type: "heading";
    position: { x: number; y: number };
    data: { label: string };
  }[] = [];
  const visited = new Set<string>();
  const columnBottoms = Array.from({ length: SECTION_COLUMNS }, () => 0);
  for (const [name, sectionRoots] of [...sections].sort(([a], [b]) => a.localeCompare(b))) {
    // Each project goes under the shortest column, so many projects stay readable.
    const column = columnBottoms.indexOf(Math.min(...columnBottoms));
    const sectionLeft = column * (SECTION_WIDTH + SECTION_GAP);
    const sectionTop = columnBottoms[column]!;
    headings.push({
      id: `heading:${name}`,
      type: "heading",
      position: { x: sectionLeft, y: sectionTop },
      data: { label: name },
    });
    let x = 0;
    let rowTop = sectionTop + HEADING_OFFSET;
    let rowHeight = 0;
    for (const root of sectionRoots) {
      const tree = layoutTree(toTree(root, childrenOf, hostNames, visited), activeId ?? "");
      const left = Math.min(...tree.nodes.map((node) => node.position.x));
      const right = Math.max(...tree.nodes.map((node) => node.position.x)) + NODE_WIDTH;
      const bottom = Math.max(...tree.nodes.map((node) => node.position.y)) + TREE_LEVEL_HEIGHT;
      if (x > 0 && x + right - left > SECTION_WIDTH) {
        x = 0;
        rowTop += rowHeight + ROW_GAP;
        rowHeight = 0;
      }
      for (const node of tree.nodes) {
        nodes.push({
          ...node,
          position: {
            x: node.position.x - left + sectionLeft + x,
            y: node.position.y + rowTop,
          },
        });
      }
      for (const edge of tree.edges) edges.push({ ...edge, label: links.get(edge.target) });
      x += right - left + TREE_GAP;
      rowHeight = Math.max(rowHeight, bottom);
    }
    columnBottoms[column] = rowTop + rowHeight + SECTION_GAP;
  }
  return { nodes, edges, headings };
}

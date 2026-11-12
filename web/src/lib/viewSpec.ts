// Agent-built views (spec v1, omnigent/views/spec.py): field binding, the JSONLogic
// subset for badges and marks, hub_ref routing, and the per-reader resolve call.
// A spec is data: nothing here runs code from it, and only own row properties are read.

import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type Tone = "neutral" | "muted" | "info" | "success" | "warning" | "danger";
export type Row = Record<string, unknown>;
export type Rule = Record<string, unknown>;

export interface ViewBadge {
  text: string;
  tone?: Tone;
  when?: Rule;
}

export interface ViewMark {
  when: Rule;
  tone?: Tone;
  label?: string;
}

export interface ViewItem {
  title: string;
  subtitle?: string;
  link?: string;
  badges?: ViewBadge[];
}

export interface ViewColumn {
  header: string;
  value: string;
  link?: string;
  badges?: ViewBadge[];
}

export interface ViewSource {
  name: string;
  params?: Record<string, unknown>;
  order?: string;
  limit?: number;
}

export interface ViewNode {
  type: string;
  source: ViewSource;
  item?: ViewItem;
  columns?: ViewColumn[];
  marks?: ViewMark[];
  empty?: string;
}

export interface ViewSpec {
  spec: 1;
  title?: string;
  view: ViewNode;
}

export interface ResolvedView {
  origin: string;
  rows: Row[];
  truncated: boolean;
}

const PATH = /^\$(\.[A-Za-z][A-Za-z0-9_]*)+$/;
const FIELD_PATH = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$/;
const MAX_RULE_DEPTH = 16;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A dotted field of a row, reading own properties only; a missing field is `null`. */
export function fieldValue(row: Row, dotted: string): unknown {
  if (!FIELD_PATH.test(dotted)) return null;
  let current: unknown = row;
  for (const part of dotted.split(".")) {
    if (!isRecord(current) || !Object.hasOwn(current, part)) return null;
    current = current[part];
  }
  return current;
}

/** `$.a.b` binds a row field; any other text is literal. */
export function bindText(row: Row, text: string): unknown {
  return PATH.test(text) ? fieldValue(row, text.slice(2)) : text;
}

/** Display text for a bound value; objects and arrays show as compact JSON. */
export function displayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function truthy(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  return Boolean(value);
}

function compare(operator: string, left: unknown, right: unknown): boolean {
  if (typeof left === "number" && typeof right === "number") {
    if (operator === "<") return left < right;
    if (operator === "<=") return left <= right;
    if (operator === ">") return left > right;
    return left >= right;
  }
  if (typeof left === "string" && typeof right === "string") {
    if (operator === "<") return left < right;
    if (operator === "<=") return left <= right;
    if (operator === ">") return left > right;
    return left >= right;
  }
  return false;
}

function evaluate(node: unknown, row: Row, depth: number): unknown {
  if (depth > MAX_RULE_DEPTH) throw new RangeError("rule is nested too deeply");
  if (Array.isArray(node)) return node.map((part) => evaluate(part, row, depth + 1));
  if (!isRecord(node)) return node;
  const keys = Object.keys(node);
  if (keys.length !== 1) throw new TypeError("a rule object has exactly one operator");
  const operator = keys[0];
  const argument = node[operator];
  if (operator === "var") {
    if (typeof argument !== "string") throw new TypeError("'var' takes a field name");
    return fieldValue(row, argument);
  }
  const raw = Array.isArray(argument) ? argument : [argument];
  const values = raw.map((part) => evaluate(part, row, depth + 1));
  switch (operator) {
    case "==":
      return values[0] === values[1];
    case "!=":
      return values[0] !== values[1];
    case "<":
    case "<=":
    case ">":
    case ">=":
      return compare(operator, values[0], values[1]);
    case "and":
      return values.every(truthy);
    case "or":
      return values.some(truthy);
    case "!":
      return !truthy(values[0]);
    case "in": {
      const [needle, haystack] = values;
      if (typeof haystack === "string" && typeof needle === "string") {
        return haystack.includes(needle);
      }
      return Array.isArray(haystack) && haystack.includes(needle);
    }
    default:
      throw new TypeError(`unknown rule operator ${operator}`);
  }
}

/**
 * Whether a rule holds for a row. Equality is strict (JSONLogic's loose `==` is not
 * copied); a malformed or too-deep rule is false, so a bad mark never hides rows.
 */
export function ruleHolds(rule: Rule | undefined, row: Row): boolean {
  if (rule === undefined) return true;
  try {
    return truthy(evaluate(rule, row, 0));
  } catch {
    return false;
  }
}

/** The web route for a hub_ref; a foreign origin, unknown kind or non-hub_ref is `null`. */
export function hubRefRoute(value: unknown): string | null {
  if (!isRecord(value) || value.origin !== "hub" || typeof value.id !== "string") return null;
  const id = encodeURIComponent(value.id);
  if (value.kind === "session") return `/c/${id}`;
  if (value.kind === "thread") return `/threads/${id}`;
  return null;
}

/** The spec a ui_render call showed: the normalized one from its output, else its argument. */
export function renderedSpec(
  args: Record<string, unknown>,
  output: string | null,
): { spec: ViewSpec | null; error: string | null } {
  let parsed: unknown = null;
  if (output !== null) {
    try {
      parsed = JSON.parse(output);
    } catch {
      return { spec: null, error: output };
    }
  }
  if (isRecord(parsed) && typeof parsed.error === "string") {
    const detail = typeof parsed.detail === "string" ? `: ${parsed.detail}` : "";
    return { spec: null, error: `${parsed.error}${detail}` };
  }
  const candidate = isRecord(parsed) && isRecord(parsed.spec) ? parsed.spec : args.spec;
  return isViewSpec(candidate)
    ? { spec: candidate, error: null }
    : { spec: null, error: output === null ? null : "The view spec is missing or malformed." };
}

function isViewSpec(value: unknown): value is ViewSpec {
  return (
    isRecord(value) &&
    value.spec === 1 &&
    isRecord(value.view) &&
    typeof value.view.type === "string" &&
    isRecord(value.view.source)
  );
}

/** Rows of a view's source for the signed-in reader. */
export async function resolveView(source: ViewSource, signal?: AbortSignal): Promise<ResolvedView> {
  const res = await authenticatedFetch("/v1/views/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
    signal,
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ResolvedView;
}

export interface TreeNode {
  row: Row;
  children: TreeNode[];
}

/**
 * Session rows as a spawned_by forest, every row exactly once and in linear time. A row
 * whose parent is not in the result is a root; a parent chain that loops is cut where
 * the walk closes it. Parents are looked up by the first row with that id.
 */
export function buildTree(rows: Row[]): TreeNode[] {
  const nodes = rows.map((row): TreeNode => ({ row, children: [] }));
  const byId = new Map<string, number>();
  nodes.forEach((node, index) => {
    const id = fieldValue(node.row, "id");
    if (typeof id === "string" && !byId.has(id)) byId.set(id, index);
  });
  const parentIndex = nodes.map((node, index) => {
    const parentId = fieldValue(node.row, "parent_actor_id");
    const parent = typeof parentId === "string" ? byId.get(parentId) : undefined;
    return parent === index ? undefined : parent;
  });
  // 0 unvisited, 1 on the current walk, 2 settled.
  const state = new Uint8Array(nodes.length);
  for (let start = 0; start < nodes.length; start += 1) {
    const walk: number[] = [];
    let current: number | undefined = start;
    while (current !== undefined && state[current] === 0) {
      state[current] = 1;
      walk.push(current);
      current = parentIndex[current];
    }
    if (current !== undefined && state[current] === 1) parentIndex[current] = undefined;
    for (const index of walk) state[index] = 2;
  }
  const roots: TreeNode[] = [];
  nodes.forEach((node, index) => {
    const parent = parentIndex[index];
    if (parent === undefined) roots.push(node);
    else nodes[parent].children.push(node);
  });
  return roots;
}

/** How many rows sit below a node. */
export function descendantCount(node: TreeNode): number {
  let count = 0;
  const pending = [...node.children];
  while (pending.length > 0) {
    const next = pending.pop()!;
    count += 1;
    pending.push(...next.children);
  }
  return count;
}

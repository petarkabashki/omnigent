// Expanded view of a ui_render call: the agent's view spec, resolved again for the
// reader through /v1/views/resolve, as a List, Table or Tree.

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import {
  type ResolvedView,
  type Row,
  type Tone,
  type TreeNode,
  type ViewBadge,
  type ViewNode,
  bindText,
  buildTree,
  descendantCount,
  displayText,
  hubRefRoute,
  renderedSpec,
  resolveView,
  ruleHolds,
} from "@/lib/viewSpec";
import type { ToolBodyProps } from "./toolViews";
import { ToolErrorBox } from "./ToolCodePanel";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "bg-muted text-foreground",
  muted: "bg-muted text-muted-foreground",
  info: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  success: "bg-green-500/15 text-green-700 dark:text-green-300",
  warning: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  danger: "bg-red-500/15 text-red-700 dark:text-red-300",
};
const MARK_CLASS: Record<Tone, string> = {
  neutral: "border-l-border",
  muted: "border-l-muted-foreground/40",
  info: "border-l-blue-500",
  success: "border-l-green-500",
  warning: "border-l-amber-500",
  danger: "border-l-red-500",
};
const MAX_TREE_DEPTH = 8;

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; view: ResolvedView };

export function ViewRenderBody({ call }: ToolBodyProps) {
  const { spec, error } = useMemo(
    () => renderedSpec(call.args, call.output),
    [call.args, call.output],
  );
  const sourceKey = spec === null ? null : JSON.stringify(spec.view.source);
  const [load, setLoad] = useState<Load>({ status: "loading" });

  useEffect(() => {
    if (sourceKey === null) return;
    const controller = new AbortController();
    setLoad({ status: "loading" });
    resolveView(JSON.parse(sourceKey), controller.signal).then(
      (view) => setLoad({ status: "ready", view }),
      (failure: unknown) => {
        if (controller.signal.aborted) return;
        const message = failure instanceof Error ? failure.message : String(failure);
        setLoad({ status: "error", message });
      },
    );
    return () => controller.abort();
  }, [sourceKey]);

  if (error !== null) return <ToolErrorBox>{error}</ToolErrorBox>;
  if (spec === null) return null;
  return (
    <div className="space-y-2" data-testid="agent-view">
      {spec.title && <p className="font-medium text-sm">{spec.title}</p>}
      {load.status === "loading" && <p className="text-muted-foreground text-sm">Loading…</p>}
      {load.status === "error" && <ToolErrorBox>{load.message}</ToolErrorBox>}
      {load.status === "ready" && <ViewNodeView node={spec.view} resolved={load.view} />}
    </div>
  );
}

function ViewNodeView({ node, resolved }: { node: ViewNode; resolved: ResolvedView }) {
  const { rows, truncated } = resolved;
  let body: ReactNode;
  if (rows.length === 0) {
    body = <p className="text-muted-foreground text-sm">{node.empty ?? "No rows match."}</p>;
  } else if (node.type === "List" && node.item) {
    body = (
      <ul className="divide-y rounded-md border">
        {rows.map((row, index) => (
          <li key={rowKey(row, index)} className={markClass(node, row)}>
            <ItemView item={node.item!} row={row} />
          </li>
        ))}
      </ul>
    );
  } else if (node.type === "Table" && node.columns) {
    body = <TableView node={node} rows={rows} />;
  } else if (node.type === "Tree" && node.item) {
    body = (
      <ul className="space-y-1">
        {buildTree(rows).map((tree, index) => (
          <TreeItem key={rowKey(tree.row, index)} node={node} tree={tree} depth={0} />
        ))}
      </ul>
    );
  } else {
    body = (
      <p className="rounded-md border border-dashed p-2 text-muted-foreground text-sm">
        Unsupported view type "{node.type}".
      </p>
    );
  }
  return (
    <>
      {body}
      {truncated && (
        <p className="text-muted-foreground text-xs">Showing the first {rows.length} rows.</p>
      )}
    </>
  );
}

function TableView({ node, rows }: { node: ViewNode; rows: Row[] }) {
  // A spec's columns never reorder, so their position is a stable key.
  const columns = (node.columns ?? []).map((column, position) => ({ column, key: `c${position}` }));
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-left">
          <tr>
            {columns.map(({ column, key }) => (
              <th key={key} className="px-2 py-1 font-medium">
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)} className={markClass(node, row)}>
              {columns.map(({ column, key }) => (
                <td key={key} className="px-2 py-1 align-top">
                  <LinkedText text={column.value} link={column.link} row={row} />
                  <Badges badges={column.badges} row={row} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TreeItem({ node, tree, depth }: { node: ViewNode; tree: TreeNode; depth: number }) {
  return (
    <li>
      <div className={cn("rounded-md border", markClass(node, tree.row))}>
        <ItemView item={node.item!} row={tree.row} />
      </div>
      {tree.children.length > 0 && depth >= MAX_TREE_DEPTH && (
        <p className="mt-1 ml-4 text-muted-foreground text-xs">
          {descendantCount(tree)} more nested rows not shown
        </p>
      )}
      {tree.children.length > 0 && depth < MAX_TREE_DEPTH && (
        <ul className="mt-1 ml-4 space-y-1 border-l pl-2">
          {tree.children.map((child, index) => (
            <TreeItem key={rowKey(child.row, index)} node={node} tree={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

function ItemView({ item, row }: { item: NonNullable<ViewNode["item"]>; row: Row }) {
  const subtitle = item.subtitle === undefined ? "" : displayText(bindText(row, item.subtitle));
  return (
    <div className="px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5 text-sm">
        <LinkedText text={item.title} link={item.link} row={row} />
        <Badges badges={item.badges} row={row} />
      </div>
      {subtitle && <p className="text-muted-foreground text-xs">{subtitle}</p>}
    </div>
  );
}

/** Bound text, linked only when `link` binds a hub_ref with a known route. */
function LinkedText({ text, link, row }: { text: string; link?: string; row: Row }) {
  const label = displayText(bindText(row, text)) || "—";
  const route = link === undefined ? null : hubRefRoute(bindText(row, link));
  if (route === null) return <span>{label}</span>;
  return (
    <Link to={route} className="text-primary underline-offset-2 hover:underline">
      {label}
    </Link>
  );
}

function Badges({ badges, row }: { badges?: ViewBadge[]; row: Row }) {
  const shown = (badges ?? [])
    .map((badge, position) => ({ badge, key: `b${position}` }))
    .filter(({ badge }) => ruleHolds(badge.when, row));
  return shown.map(({ badge, key }) => (
    <span
      key={key}
      className={cn("rounded px-1.5 py-0.5 text-xs", TONE_CLASS[badge.tone ?? "neutral"])}
    >
      {displayText(bindText(row, badge.text))}
    </span>
  ));
}

function markClass(node: ViewNode, row: Row): string | undefined {
  const mark = (node.marks ?? []).find((candidate) => ruleHolds(candidate.when, row));
  return mark === undefined ? undefined : cn("border-l-4", MARK_CLASS[mark.tone ?? "warning"]);
}

/** Row ids can repeat, so the position keeps keys unique; rows are not reordered in place. */
function rowKey(row: Row, index: number): string {
  const id = row.id;
  return typeof id === "string" ? `${id}:${index}` : `row-${index}`;
}

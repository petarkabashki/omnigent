// Pure helpers that group the flat `/v1/projects` list into a namespace tree
// (`parent_id` links, server-computed `path`). The nested sidebar renders from
// this; legacy projects whose name contains "/" stay roots with a literal name.

/** The fields the tree needs; satisfied by both `Project` and the sidebar's `ProjectSummary`. */
export interface TreeProject {
  /** `null` for a legacy label-only project, which is always a root. */
  id: string | null;
  name: string;
  parent_id?: string | null;
  path?: string | null;
}

export interface ProjectTreeNode<T extends TreeProject = TreeProject> {
  project: T;
  /** Namespace path, falling back to the project name when the server sent none. */
  path: string;
  depth: number;
  children: ProjectTreeNode<T>[];
}

/** The namespace path that identifies a project; a root's path is its name. */
export function projectPathOf(project: Pick<TreeProject, "name" | "path">): string {
  return project.path ?? project.name;
}

/** Whether a picker may offer "Create" for `typed`: a new project is one segment, never a path. */
export function canCreateProjectNamed(
  projects: Pick<TreeProject, "name" | "path">[],
  typed: string,
): boolean {
  const wanted = typed.toLowerCase();
  return (
    wanted.length > 0 &&
    !wanted.includes("/") &&
    !projects.some((project) => projectPathOf(project).toLowerCase() === wanted)
  );
}

/**
 * Build the forest of projects, keeping the input order among siblings.
 * A project whose parent is missing from the list is treated as a root.
 */
export function buildProjectTree<T extends TreeProject>(projects: T[]): ProjectTreeNode<T>[] {
  const ids = new Set(projects.map((project) => project.id));
  const childrenOf = new Map<string, T[]>();
  const roots: T[] = [];
  for (const project of projects) {
    const parentId = project.parent_id;
    if (project.id && parentId && ids.has(parentId) && parentId !== project.id) {
      childrenOf.set(parentId, [...(childrenOf.get(parentId) ?? []), project]);
    } else {
      roots.push(project);
    }
  }
  const visited = new Set<T>();
  const build = (project: T, depth: number): ProjectTreeNode<T> => {
    visited.add(project);
    return {
      project,
      path: project.path ?? project.name,
      depth,
      children: (project.id ? (childrenOf.get(project.id) ?? []) : [])
        .filter((child) => !visited.has(child))
        .map((child) => build(child, depth + 1)),
    };
  };
  return roots.map((root) => build(root, 0));
}

/** Flatten the tree depth-first, for indented list rendering and path pickers. */
export function flattenProjectTree<T extends TreeProject>(
  nodes: ProjectTreeNode<T>[],
): ProjectTreeNode<T>[] {
  return nodes.flatMap((node) => [node, ...flattenProjectTree(node.children)]);
}

/** The ids of `id` and every project nested under it, for excluding move targets. */
export function projectSubtreeIds(nodes: ProjectTreeNode[], id: string): Set<string> {
  const found = new Set<string>();
  const walk = (node: ProjectTreeNode, inside: boolean) => {
    const here = inside || node.project.id === id;
    if (here && node.project.id) found.add(node.project.id);
    node.children.forEach((child) => walk(child, here));
  };
  nodes.forEach((node) => walk(node, false));
  return found;
}

/** Breadcrumb segments for a namespace path (`a/b/c` -> `["a", "b", "c"]`). */
export function pathSegments(path: string): string[] {
  return path.split("/").filter((segment) => segment !== "");
}

/** A path and every path enclosing it (`a/b/c` -> `["a", "a/b", "a/b/c"]`). */
export function pathPrefixes(path: string): string[] {
  return path.split("/").map((_, index, parts) => parts.slice(0, index + 1).join("/"));
}

/** Projects sharing `project`'s parent; an orphan counts as a root, as in the tree. */
export function projectSiblings<T extends TreeProject>(projects: T[], project: T): T[] {
  const known = new Set(projects.map((p) => p.id));
  const parentOf = (p: T) => (p.parent_id && known.has(p.parent_id) ? p.parent_id : null);
  return projects.filter((p) => parentOf(p) === parentOf(project));
}

/**
 * The flat list after moving the project at path `key` among its siblings, or
 * `null` when it is already there, so callers never save a move that changes nothing.
 */
export function moveAmongSiblings<T extends TreeProject>(
  projects: T[],
  key: string,
  destination: "up" | "down" | "top" | "bottom",
): T[] | null {
  const moved = projects.find((p) => projectPathOf(p) === key);
  if (!moved) return null;
  const siblings = projectSiblings(projects, moved);
  const position = siblings.indexOf(moved);
  const targetPosition =
    destination === "top"
      ? 0
      : destination === "bottom"
        ? siblings.length - 1
        : destination === "up"
          ? position - 1
          : position + 1;
  const target = siblings[targetPosition];
  if (!target || target === moved) return null;
  const next = projects.filter((p) => p !== moved);
  next.splice(next.indexOf(target) + (position < targetPosition ? 1 : 0), 0, moved);
  return next;
}

import { describe, expect, it } from "vitest";
import { buildProjectTree, flattenProjectTree } from "./projectTree";
import type { Project } from "./projectsApi";

const project = (id: string, name: string, parent_id?: string, path?: string): Project => ({
  id,
  name,
  parent_id: parent_id ?? null,
  path: path ?? name,
});

describe("buildProjectTree", () => {
  it("nests children under their parent and keeps sibling order", () => {
    const tree = buildProjectTree([
      project("a", "a"),
      project("b", "b", "a", "a/b"),
      project("z", "z"),
      project("c", "c", "a", "a/c"),
    ]);
    expect(tree.map((node) => node.project.id)).toEqual(["a", "z"]);
    expect(tree[0].children.map((node) => [node.path, node.depth])).toEqual([
      ["a/b", 1],
      ["a/c", 1],
    ]);
  });

  it("treats a project with a missing parent, or a legacy slash name, as a root", () => {
    const tree = buildProjectTree([project("x", "x/y"), project("o", "o", "gone")]);
    expect(tree.map((node) => node.path)).toEqual(["x/y", "o"]);
  });

  it("does not loop on a cycle", () => {
    const tree = buildProjectTree([project("a", "a", "b"), project("b", "b", "a")]);
    expect(tree).toEqual([]);
  });

  it("flattens depth-first", () => {
    const flat = flattenProjectTree(
      buildProjectTree([project("a", "a"), project("b", "b", "a"), project("z", "z")]),
    );
    expect(flat.map((node) => node.project.id)).toEqual(["a", "b", "z"]);
  });
});

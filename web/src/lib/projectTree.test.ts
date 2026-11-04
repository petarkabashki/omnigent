import { describe, expect, it } from "vitest";
import {
  buildProjectTree,
  flattenProjectTree,
  moveAmongSiblings,
  pathPrefixes,
} from "./projectTree";
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

describe("moveAmongSiblings", () => {
  const flat = [
    project("a", "a"),
    project("a1", "x", "a", "a/x"),
    project("a2", "y", "a", "a/y"),
    project("b", "b"),
    project("c", "c"),
  ];
  const order = (key: string, destination: "up" | "down" | "top" | "bottom") =>
    moveAmongSiblings(flat, key, destination)?.map((p) => p.id);

  it("moves past the next sibling, not the row listed next", () => {
    expect(order("a", "down")).toEqual(["a1", "a2", "b", "a", "c"]);
    expect(order("b", "up")).toEqual(["b", "a", "a1", "a2", "c"]);
  });

  it("keeps top and bottom within the siblings", () => {
    expect(order("c", "top")).toEqual(["c", "a", "a1", "a2", "b"]);
    expect(order("a", "bottom")).toEqual(["a1", "a2", "b", "c", "a"]);
    expect(order("a/x", "bottom")).toEqual(["a", "a2", "a1", "b", "c"]);
    expect(order("a/y", "top")).toEqual(["a", "a2", "a1", "b", "c"]);
  });

  it("returns null at a sibling bound instead of a move that changes nothing", () => {
    expect(order("a", "up")).toBeUndefined();
    expect(order("c", "down")).toBeUndefined();
    expect(order("a/x", "top")).toBeUndefined();
    expect(order("a/y", "bottom")).toBeUndefined();
    expect(order("missing", "up")).toBeUndefined();
  });

  it("treats an orphan as a root, as the tree does", () => {
    const orphan = project("o", "o", "gone");
    const moved = moveAmongSiblings([project("a", "a"), orphan], "o", "up");
    expect(moved?.map((p) => p.id)).toEqual(["o", "a"]);
  });
});

describe("pathPrefixes", () => {
  it("lists a path and everything enclosing it", () => {
    expect(pathPrefixes("a/b/c")).toEqual(["a", "a/b", "a/b/c"]);
    expect(pathPrefixes("root")).toEqual(["root"]);
  });
});

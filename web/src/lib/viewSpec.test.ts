import { describe, expect, it } from "vitest";
import { bindText, buildTree, hubRefRoute, renderedSpec, ruleHolds } from "./viewSpec";

const row = {
  id: "s1",
  title: "Desk",
  stopped: false,
  depth: 2,
  roles: ["/Product"],
  hub_ref: { origin: "hub", kind: "session", id: "s1" },
};

describe("bindText", () => {
  it("binds $.paths, reads own properties only and keeps other text literal", () => {
    expect(bindText(row, "$.title")).toBe("Desk");
    expect(bindText(row, "$.hub_ref.kind")).toBe("session");
    expect(bindText(row, "$.missing")).toBeNull();
    expect(bindText(row, "$.constructor")).toBeNull();
    expect(bindText(row, "$.title.length")).toBeNull();
    expect(bindText(row, "plain $.title")).toBe("plain $.title");
  });
});

describe("ruleHolds", () => {
  it.each([
    [{ "==": [{ var: "stopped" }, false] }, true],
    [{ "==": [{ var: "depth" }, "2"] }, false],
    [{ ">=": [{ var: "depth" }, 2] }, true],
    [{ and: [{ var: "title" }, { "!": [{ var: "stopped" }] }] }, true],
    [{ or: [{ var: "stopped" }, { var: "missing" }] }, false],
    [{ in: ["/Product", { var: "roles" }] }, true],
    [{ in: ["es", { var: "title" }] }, true],
    [{ "<": [{ var: "title" }, 3] }, false],
    [{ eval: ["x"] }, false],
    [{ var: "__proto__" }, false],
    [{ "==": [1, 1], "!=": [1, 2] }, false],
  ])("%j is %s", (rule, expected) => {
    expect(ruleHolds(rule, row)).toBe(expected);
  });

  it("is true without a rule and false past the depth cap", () => {
    expect(ruleHolds(undefined, row)).toBe(true);
    let deep: Record<string, unknown> = { var: "title" };
    for (let index = 0; index < 20; index += 1) deep = { "!": [deep] };
    expect(ruleHolds(deep, row)).toBe(false);
  });
});

describe("hubRefRoute", () => {
  it("routes hub sessions and threads and leaves anything else as text", () => {
    expect(hubRefRoute(row.hub_ref)).toBe("/c/s1");
    expect(hubRefRoute({ origin: "hub", kind: "thread", id: "t/1" })).toBe("/threads/t%2F1");
    expect(hubRefRoute({ origin: "elsewhere", kind: "session", id: "s1" })).toBeNull();
    expect(hubRefRoute({ origin: "hub", kind: "task", id: "x" })).toBeNull();
    expect(hubRefRoute("s1")).toBeNull();
  });
});

describe("renderedSpec", () => {
  const spec = { spec: 1, view: { type: "List", source: { name: "sessions" } } };

  it("prefers the normalized output spec and reports a tool error", () => {
    const normalized = { ...spec, title: "Mine" };
    expect(renderedSpec({ spec }, JSON.stringify({ spec: normalized, rows: 1 })).spec).toEqual(
      normalized,
    );
    expect(renderedSpec({ spec }, null)).toEqual({ spec, error: null });
    expect(
      renderedSpec({ spec }, JSON.stringify({ error: "invalid view spec", detail: "bad" })).error,
    ).toBe("invalid view spec: bad");
    expect(renderedSpec({ spec: "nope" }, "{}").spec).toBeNull();
  });
});

describe("buildTree", () => {
  it("nests by parent_actor_id and keeps every row of a cycle once", () => {
    const tree = buildTree([
      { id: "a", parent_actor_id: "root" },
      { id: "b", parent_actor_id: "a" },
      { id: "x", parent_actor_id: "y" },
      { id: "y", parent_actor_id: "x" },
    ]);
    expect(tree.map((node) => node.row.id)).toEqual(["a", "x", "y"]);
    expect(tree[0].children.map((node) => node.row.id)).toEqual(["b"]);
  });
});

import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { buildSessionGraph, sessionLink, sessionLocation } from "./sessionGraph";

function session(id: string, fields: Partial<Conversation> = {}): Conversation {
  return { id, title: id, labels: {}, ...fields } as Conversation;
}

const options = { hostNames: new Map(), projectNames: new Map(), activeId: null };

describe("sessionLink", () => {
  it("prefers the dispatcher over a fork source", () => {
    const lane = session("lane", {
      labels: { "ag-fleet.dispatched_by": "coordinator", "omnigent.fork.source_id": "old" },
    });
    expect(sessionLink(lane, new Set(["coordinator", "old"]))).toEqual({
      sourceId: "coordinator",
      kind: "dispatched",
    });
  });

  it("ignores a source that is not on the graph, and a session pointing at itself", () => {
    const lane = session("lane", { labels: { "ag-fleet.dispatched_by": "gone" } });
    expect(sessionLink(lane, new Set(["lane"]))).toBeNull();
    const selfLinked = session("lane", { labels: { "ag-fleet.dispatched_by": "lane" } });
    expect(sessionLink(selfLinked, new Set(["lane"]))).toBeNull();
  });
});

describe("sessionLocation", () => {
  it("names the machine from the fleet label, then from the host", () => {
    const hosts = new Map([["host_1", "desktop"]]);
    expect(
      sessionLocation(
        session("a", { labels: { "ag-fleet.host": "gpu26" }, workspace: "/w/repo" }),
        hosts,
      ),
    ).toBe("gpu26 · repo");
    expect(sessionLocation(session("b", { host_id: "host_1" }), hosts)).toBe("desktop");
    expect(sessionLocation(session("c"), hosts)).toBe("No machine");
  });
});

describe("buildSessionGraph", () => {
  it("hangs a lane under its dispatcher, in the dispatcher's project", () => {
    const graph = buildSessionGraph(
      [
        session("coordinator", { project_id: "p1" }),
        session("lane", { project_id: "p2", labels: { "ag-fleet.dispatched_by": "coordinator" } }),
      ],
      {
        ...options,
        projectNames: new Map([
          ["p1", "omnigent"],
          ["p2", "other"],
        ]),
      },
    );
    expect(graph.headings.map((heading) => heading.data.label)).toEqual(["omnigent"]);
    expect(graph.edges).toMatchObject([
      { source: "coordinator", target: "lane", label: "dispatched" },
    ]);
  });

  it("marks a fleet row that nothing runs as a remote task", () => {
    const row = session("row", { labels: { "ag-fleet.task": "tsk_1" } });
    expect(buildSessionGraph([row], options).nodes[0]!.data.statusLabel).toBe("Remote task");
  });

  it("keeps two sessions that name each other", () => {
    const graph = buildSessionGraph(
      [
        session("a", { labels: { "omnigent.fork.source_id": "b" } }),
        session("b", { labels: { "omnigent.fork.source_id": "a" } }),
      ],
      options,
    );
    expect(graph.nodes.map((node) => node.id).sort()).toEqual(["a", "b"]);
  });

  it("does not overlap unrelated sessions", () => {
    const sessions = Array.from({ length: 20 }, (_, index) => session(`s${index}`));
    const spots = buildSessionGraph(sessions, options).nodes.map(
      (node) => `${node.position.x},${node.position.y}`,
    );
    expect(new Set(spots).size).toBe(20);
  });
});

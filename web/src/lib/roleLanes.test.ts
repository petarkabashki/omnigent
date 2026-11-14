import { describe, expect, it } from "vitest";
import { ALIAS_LABEL_KEY } from "@/lib/rolesApi";
import {
  groupRoleLanes,
  LANE_NAME_LABEL_KEY,
  ROLE_LANE_LABEL_KEY,
  roleLaneName,
} from "@/lib/roleLanes";

const projects = new Map([["proj_1", "acme"]]);

function row(id: string, labels: Record<string, string> = {}, projectId: string | null = "proj_1") {
  return { id, project_id: projectId, labels };
}

const holder = row("holder", { [ALIAS_LABEL_KEY]: "Architect" });
const lane = (id: string, handle = "acme/Architect") => row(id, { [ROLE_LANE_LABEL_KEY]: handle });

describe("groupRoleLanes", () => {
  it("nests lanes under their holder in input order", () => {
    const groups = groupRoleLanes([lane("b"), holder, lane("a"), row("plain")], projects);
    expect(groups.lanesByHolderId.get("holder")?.map((r) => r.id)).toEqual(["b", "a"]);
    expect([...groups.laneIds]).toEqual(["b", "a"]);
  });

  it("matches the handle case-insensitively", () => {
    const groups = groupRoleLanes([holder, lane("a", " ACME/architect ")], projects);
    expect(groups.laneIds.has("a")).toBe(true);
  });

  it("leaves a lane top level when its role has no row (vacant, missing, filtered out)", () => {
    const groups = groupRoleLanes([lane("a"), lane("b", "acme/Product")], projects);
    expect(groups.laneIds.size).toBe(0);
    expect(groups.lanesByHolderId.size).toBe(0);
  });

  it("does not match a role of the same name in another project", () => {
    const other = row("other", { [ALIAS_LABEL_KEY]: "Architect" }, null);
    const groups = groupRoleLanes([other, lane("a")], projects);
    expect(groups.laneIds.size).toBe(0);
  });

  it("matches unfiled roles by the bare /Alias handle", () => {
    const unfiled = row("unfiled", { [ALIAS_LABEL_KEY]: "Scratch" }, null);
    const groups = groupRoleLanes([unfiled, lane("a", "/Scratch")], projects);
    expect(groups.lanesByHolderId.get("unfiled")?.map((r) => r.id)).toEqual(["a"]);
  });

  it("never nests a role holder, so nesting stays one level deep", () => {
    const holderAndLane = row("bench", {
      [ALIAS_LABEL_KEY]: "Bench",
      [ROLE_LANE_LABEL_KEY]: "acme/Architect",
    });
    const groups = groupRoleLanes([holder, holderAndLane, lane("a", "acme/Bench")], projects);
    expect(groups.laneIds.has("bench")).toBe(false);
    expect(groups.lanesByHolderId.get("bench")?.map((r) => r.id)).toEqual(["a"]);
  });

  it.each([
    ["c30", "c30"],
    [" ui-polish ", "ui-polish"],
    ["a--b", "a--b"],
    ["x".repeat(32), "x".repeat(32)],
    ["UI", null],
    ["*", null],
    ["a_b", null],
    ["x".repeat(33), null],
    ["", null],
  ])("reads lane name %j as %j", (value, expected) => {
    expect(roleLaneName({ [LANE_NAME_LABEL_KEY]: value })).toBe(expected);
  });

  it("waits for the project path before matching a filed role", () => {
    const groups = groupRoleLanes([holder, lane("a")], new Map());
    expect(groups.laneIds.size).toBe(0);
  });
});

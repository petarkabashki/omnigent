import { describe, expect, it } from "vitest";
import { newThreadPath } from "@/lib/groupThreads";
import { filterMemberOptions, type MemberOption } from "./ThreadMemberPicker";

const OPTIONS: MemberOption[] = [
  { kind: "role", ref: "omnigent/Product", label: "omnigent/Product", detail: "role" },
  { kind: "session", ref: "s1", label: "Release notes", detail: "session s1" },
];

describe("filterMemberOptions", () => {
  it("matches label or detail case-insensitively and skips chosen members", () => {
    expect(filterMemberOptions(OPTIONS, "PROD", []).map((o) => o.ref)).toEqual([
      "omnigent/Product",
    ]);
    expect(filterMemberOptions(OPTIONS, "session", []).map((o) => o.ref)).toEqual(["s1"]);
    expect(filterMemberOptions(OPTIONS, "", [{ kind: "session", ref: "s1" }])).toHaveLength(1);
  });
});

describe("newThreadPath", () => {
  it("names each member in a with parameter", () => {
    expect(newThreadPath(["omnigent/Product", "/Bench"])).toBe(
      "/threads?with=omnigent%2FProduct&with=%2FBench",
    );
  });
});

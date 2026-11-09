import { describe, expect, it } from "vitest";
import { roleThreadsFromLabels } from "./roleThreads";

const ALIAS = { "omnigent.alias": "Product" };

describe("roleThreadsFromLabels", () => {
  it("reads the thread index, urgent states first, then newest", () => {
    const rows = roleThreadsFromLabels({
      ...ALIAS,
      "thread.aaaaaaaaaaaa.cp": "/Architect",
      "thread.aaaaaaaaaaaa.at": "300",
      "thread.aaaaaaaaaaaa.n": "4",
      "thread.bbbbbbbbbbbb.cp": "/Bench",
      "thread.bbbbbbbbbbbb.state": "waiting_on_us",
      "thread.bbbbbbbbbbbb.unread": "2",
      "thread.bbbbbbbbbbbb.sum": "Needs numbers",
      "thread.bbbbbbbbbbbb.at": "100",
      "thread.cccccccccccc.cp": "session:abc",
      "thread.cccccccccccc.at": "200",
      "thread.role": "/Product",
    });
    expect(rows.map((row) => row.counterpart)).toEqual(["/Bench", "/Architect", "session:abc"]);
    expect(rows[0]).toMatchObject({ state: "waiting_on_us", unread: 2, summary: "Needs numbers" });
    expect(rows[1]).toMatchObject({ state: "open", unread: 0, messages: 4 });
  });

  it("is empty for a session without a role or without a counterpart", () => {
    expect(roleThreadsFromLabels({ "thread.aaaaaaaaaaaa.cp": "/Architect" })).toEqual([]);
    expect(roleThreadsFromLabels({ ...ALIAS, "thread.aaaaaaaaaaaa.n": "3" })).toEqual([]);
    expect(roleThreadsFromLabels(undefined)).toEqual([]);
  });
});

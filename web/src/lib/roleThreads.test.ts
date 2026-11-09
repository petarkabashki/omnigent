import { describe, expect, it } from "vitest";
import { roleHandlePath, roleThreadStateLabels, roleThreadsFromLabels } from "./roleThreads";

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

describe("roleThreadStateLabels", () => {
  const thread = roleThreadsFromLabels({ ...ALIAS, "thread.aaaaaaaaaaaa.cp": "/Bench" })[0];

  it.each([
    [
      "done",
      {
        "thread.aaaaaaaaaaaa.state": "done",
        "thread.aaaaaaaaaaaa.at": "9",
        "thread.aaaaaaaaaaaa.unread": "0",
      },
    ],
    ["open", { "thread.aaaaaaaaaaaa.state": "open", "thread.aaaaaaaaaaaa.at": "9" }],
  ])("sets %s on the thread's own keys", (state, expected) => {
    expect(roleThreadStateLabels(thread, state, 9)).toEqual(expected);
  });
});

describe("roleHandlePath", () => {
  it.each([
    ["forthkit/Product", "forthkit"],
    ["/Bench", ""],
    ["a/b/Role", "a/b"],
  ])("%s is in %j", (handle, path) => {
    expect(roleHandlePath(handle)).toBe(path);
  });
});

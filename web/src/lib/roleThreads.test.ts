import { afterEach, describe, expect, it, vi } from "vitest";
import * as identity from "@/lib/identity";
import {
  roleHandlePath,
  roleThreadIsUrgent,
  roleThreadStateLabels,
  roleThreadStateText,
  resolveHeldThreadMessage,
  roleThreadsFromLabels,
} from "./roleThreads";

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

  it("puts a thread that needs the owner first, shown by name", () => {
    const rows = roleThreadsFromLabels({
      ...ALIAS,
      "thread.aaaaaaaaaaaa.cp": "/Architect",
      "thread.aaaaaaaaaaaa.state": "waiting_on_us",
      "thread.aaaaaaaaaaaa.at": "300",
      "thread.bbbbbbbbbbbb.cp": "/Bench",
      "thread.bbbbbbbbbbbb.state": "waiting_on_owner",
      "thread.bbbbbbbbbbbb.at": "100",
    });
    expect(rows.map((row) => row.counterpart)).toEqual(["/Bench", "/Architect"]);
    expect(roleThreadStateText("waiting_on_owner")).toBe("Needs you");
    expect(
      ["waiting_on_owner", "blocked", "waiting_on_us", "open"].map(roleThreadIsUrgent),
    ).toEqual([true, true, true, false]);
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

describe("held thread message", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses the held labels, with underscores in field names", () => {
    const [row] = roleThreadsFromLabels({
      ...ALIAS,
      "thread.aaaaaaaaaaaa.cp": "/Architect",
      "thread.aaaaaaaaaaaa.state": "waiting_on_owner",
      "thread.aaaaaaaaaaaa.held": "Please reply",
      "thread.aaaaaaaaaaaa.held_to": "sess_x",
      "thread.aaaaaaaaaaaa.held_at": "123",
    });
    expect(row.held).toEqual({ text: "Please reply", to: "sess_x", at: 123 });
    const [plain] = roleThreadsFromLabels({ ...ALIAS, "thread.aaaaaaaaaaaa.cp": "/Architect" });
    expect(plain.held).toBeUndefined();
  });

  it("posts the action for the counterpart", async () => {
    const fetchSpy = vi
      .spyOn(identity, "authenticatedFetch")
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await resolveHeldThreadMessage("sess a", "/Architect", "allow");
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/v1/sessions/sess%20a/threads/held");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      counterpart: "/Architect",
      action: "allow",
    });
  });
});

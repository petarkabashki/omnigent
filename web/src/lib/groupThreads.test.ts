import { describe, expect, it } from "vitest";
import { memberFromText, memberLabel, postAuthorLabel } from "./groupThreads";

describe("group thread members", () => {
  it("reads a session id as a session and anything else as a role handle", () => {
    expect(memberFromText(" omnigent/Product ")).toEqual({ kind: "role", ref: "omnigent/Product" });
    expect(memberFromText("/Architect")).toEqual({ kind: "role", ref: "/Architect" });
    expect(memberFromText("A".repeat(32))).toEqual({ kind: "session", ref: "A".repeat(32) });
    expect(memberFromText("0f8fad5b-d9cb-469f-a165-70867728950e").kind).toBe("session");
  });

  it("names roles by handle, sessions by short id, and the owner as You", () => {
    expect(memberLabel({ kind: "role", ref: "x/Bench" })).toBe("x/Bench");
    expect(memberLabel({ kind: "session", ref: "0123456789abcdef" })).toBe("session 01234567");
    const post = {
      id: "p",
      seq: 1,
      authorRef: "owner",
      text: "",
      createdAt: 0,
      mentions: [],
    };
    expect(postAuthorLabel({ ...post, authorKind: "owner" })).toBe("You");
    expect(postAuthorLabel({ ...post, authorKind: "role", authorRef: "x/Bench" })).toBe("x/Bench");
  });
});

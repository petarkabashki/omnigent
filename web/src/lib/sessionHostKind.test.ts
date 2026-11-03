import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { classifySessionHost, orderHostGroups } from "./sessionHostKind";

const host = (over: Partial<Host>): Host =>
  ({ host_id: "h", name: "h", status: "online", ...over }) as Host;
const hosts = new Map<string, Host>([
  ["me", host({ host_id: "me", name: "laptop" })],
  ["srv", host({ host_id: "srv", name: "build-box", status: "offline" })],
  ["sb", host({ host_id: "sb", name: "sb1", sandbox_provider: "docker" })],
]);

describe("classifySessionHost", () => {
  it("treats a missing host id as this device", () => {
    expect(classifySessionHost({ host_id: null }, hosts, null).kind).toBe("local");
  });
  it("detects this device by host id", () => {
    expect(classifySessionHost({ host_id: "me" }, hosts, "me").kind).toBe("local");
  });
  it("names remote hosts and flags offline", () => {
    expect(classifySessionHost({ host_id: "srv" }, hosts, "me")).toMatchObject({
      kind: "remote",
      name: "build-box",
      offline: true,
    });
  });
  it("falls back to the id for unknown hosts and is not offline", () => {
    expect(classifySessionHost({ host_id: "zz" }, hosts, "me")).toMatchObject({
      kind: "remote",
      name: "zz",
      offline: false,
    });
  });
  it("classifies sandboxes", () => {
    expect(classifySessionHost({ host_id: "sb" }, hosts, "me")).toMatchObject({
      kind: "sandbox",
      sandboxProvider: "docker",
    });
  });
});

describe("orderHostGroups", () => {
  it("puts this device first, hosts alphabetical, sandboxes last", () => {
    expect(
      orderHostGroups([
        { label: "Docker", kind: "sandbox" },
        { label: "zeta", kind: "remote" },
        { label: "Mac", kind: "local" },
        { label: "alpha", kind: "remote" },
        { label: "zeta", kind: "remote" },
      ]),
    ).toEqual(["Mac", "alpha", "zeta", "Docker"]);
  });
});

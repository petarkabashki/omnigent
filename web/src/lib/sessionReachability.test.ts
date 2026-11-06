import { describe, expect, it } from "vitest";
import {
  sessionReachability,
  sessionReachabilityDetail,
  sessionReachabilityGlyph,
  sessionReachabilityPopup,
  sessionReachabilityWord,
} from "./sessionReachability";

describe("sessionReachability", () => {
  it("treats a live runner as online and an unseen poll as unknown", () => {
    expect(sessionReachability({ runner_online: true, host_online: false })).toBe("online");
    expect(sessionReachability({})).toBe("unknown");
    expect(sessionReachability({ runner_online: null, host_online: false })).toBe("unknown");
    expect(sessionReachabilityWord("online")).toBeNull();
    expect(sessionReachabilityPopup({ runner_online: true })).toBe("Online");
    expect(sessionReachabilityPopup({})).toBeNull();
    expect(sessionReachabilityWord("unknown")).toBeNull();
    expect(sessionReachabilityGlyph("online")).toBeNull();
    expect(sessionReachabilityGlyph("unknown")).toBeNull();
  });

  it("calls a down runner on a live host asleep, including a sub-agent", () => {
    expect(sessionReachability({ runner_online: false, host_online: true })).toBe("asleep");
    expect(
      sessionReachability({ runner_online: false, host_online: false, kind: "sub_agent" }),
    ).toBe("asleep");
    expect(sessionReachabilityWord("asleep")).toBe("Asleep");
    expect(sessionReachabilityGlyph("asleep")?.glyph).toBe("☾");
    expect(sessionReachabilityDetail({ runner_online: false, host_online: true })).toBe(
      "Runner is asleep. A message wakes it.",
    );
    expect(sessionReachabilityPopup({ runner_online: false, host_online: true })).toBe(
      "Asleep. A message wakes it.",
    );
  });

  it("calls a down host or a stranded runner offline", () => {
    expect(sessionReachability({ runner_online: false, host_online: false })).toBe("offline");
    expect(sessionReachability({ runner_online: false, host_online: null })).toBe("offline");
    expect(sessionReachability({ runner_online: false })).toBe("unknown");
    expect(sessionReachabilityWord("offline")).toBe("Offline");
    expect(sessionReachabilityGlyph("offline")?.glyph).toBe("⊘");
    expect(sessionReachabilityDetail({ runner_online: false, host_online: false })).toBe(
      "Host is offline.",
    );
    expect(sessionReachabilityDetail({ runner_online: false, host_online: null })).toBe(
      "Agent is disconnected.",
    );
    expect(sessionReachabilityPopup({ runner_online: false, host_online: false })).toBe("Offline");
    expect(sessionReachabilityPopup({ runner_online: false, host_online: null })).toBe(
      "Disconnected",
    );
  });
});

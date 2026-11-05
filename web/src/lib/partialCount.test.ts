import { describe, expect, it } from "vitest";
import { agentsBadgeLabel, partialCountLabel } from "./partialCount";

describe("partialCountLabel", () => {
  it("marks a count taken from a partial list as a lower bound", () => {
    expect(partialCountLabel(20, true)).toBe("20+");
    expect(partialCountLabel(20, false)).toBe("20");
  });
});

describe("agentsBadgeLabel", () => {
  it("marks both figures partial when older agents are not loaded", () => {
    expect(agentsBadgeLabel(2, 21, true)).toBe("2+/21+");
    expect(agentsBadgeLabel(0, 21, true)).toBe("21+");
  });

  it("stays plain for a complete list", () => {
    expect(agentsBadgeLabel(2, 5, false)).toBe("2/5");
    expect(agentsBadgeLabel(0, 5, false)).toBe("5");
  });
});

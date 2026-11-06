import { describe, expect, it } from "vitest";
import { sessionLimit, sessionLimitDetail, sessionLimitGlyph } from "./sessionLimit";

const NOW_MS = 1_793_900_000_000;
const FUTURE = String(1_793_916_000);
const PAST = String(1_793_800_000);

const labels = (code: string, resetsAt?: string) => ({
  "omnigent.last_task_error_code": code,
  ...(resetsAt === undefined ? {} : { "omnigent.last_task_error_resets_at": resetsAt }),
});

describe("sessionLimit", () => {
  it("marks a usage limit with a future reset", () => {
    expect(sessionLimit(labels("usage_limit_reached", FUTURE), NOW_MS)).toEqual({
      resetsAt: 1_793_916_000,
    });
  });

  it("shows nothing once the reset has passed", () => {
    expect(sessionLimit(labels("usage_limit_reached", PAST), NOW_MS)).toBeNull();
  });

  it("keeps the marker, without a time, when the reset is empty or malformed", () => {
    expect(sessionLimit(labels("usage_limit_reached", ""), NOW_MS)).toEqual({ resetsAt: null });
    expect(sessionLimit(labels("usage_limit_reached"), NOW_MS)).toEqual({ resetsAt: null });
    expect(sessionLimit(labels("usage_limit_reached", "soon"), NOW_MS)).toEqual({
      resetsAt: null,
    });
  });

  it("ignores other failures, cleared labels, and missing labels", () => {
    expect(sessionLimit(labels("native_turn_error", FUTURE), NOW_MS)).toBeNull();
    expect(sessionLimit(labels("", ""), NOW_MS)).toBeNull();
    expect(sessionLimit(undefined, NOW_MS)).toBeNull();
  });

  it("words the tooltip with or without a reset time", () => {
    expect(sessionLimitDetail({ resetsAt: null })).toBe("Usage limit reached.");
    expect(sessionLimitDetail({ resetsAt: 1_793_916_000 })).toMatch(
      /^Usage limit reached\. Resets .+\.$/,
    );
    expect(sessionLimitGlyph().glyph).toBe("◔");
  });
});

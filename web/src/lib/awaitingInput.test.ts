import { describe, expect, it } from "vitest";
import {
  AWAITING_INPUT_LABEL_KEY,
  awaitingInputReason,
  needsResponse,
  needsResponseCount,
} from "./awaitingInput";

const flagged = { labels: { [AWAITING_INPUT_LABEL_KEY]: "Should I push?" } };

describe("needsResponse", () => {
  it("is true for a pending approval or an awaiting-input label", () => {
    expect(needsResponse({ pending_elicitations_count: 1 })).toBe(true);
    expect(needsResponse(flagged)).toBe(true);
  });

  it("is false for nothing, an empty (cleared) label, or a dead runner", () => {
    expect(needsResponse({ labels: {} })).toBe(false);
    expect(needsResponse({ labels: { [AWAITING_INPUT_LABEL_KEY]: "" } })).toBe(false);
    expect(needsResponse({ ...flagged, runner_online: false })).toBe(false);
  });
});

describe("awaitingInputReason / needsResponseCount", () => {
  it("returns the reason and counts it once on top of approvals", () => {
    expect(awaitingInputReason(flagged)).toBe("Should I push?");
    expect(needsResponseCount({ ...flagged, pending_elicitations_count: 2 })).toBe(3);
    expect(needsResponseCount({ labels: {} })).toBe(0);
  });
});

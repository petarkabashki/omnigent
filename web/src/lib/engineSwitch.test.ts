import { describe, expect, it } from "vitest";
import { harnessSupportsEngineSwitch, switchRefusalOf } from "./engineSwitch";

describe("harnessSupportsEngineSwitch", () => {
  it.each(["claude-sdk", "pi", "claude-native", "codex-native"])("offers %s", (harness) => {
    expect(harnessSupportsEngineSwitch(harness)).toBe(true);
  });

  it.each(["pi-native", "cursor-native", "goose-native", null, undefined, ""])(
    "withholds %s",
    (harness) => {
      expect(harnessSupportsEngineSwitch(harness)).toBe(false);
    },
  );
});

describe("switchRefusalOf", () => {
  it.each([
    ["pending_inputs", "queued; retry with pending='drop'", "pending"],
    ["session_busy", "The session is running; retry with stop_running=true", "running"],
    ["session_busy", "1 running sub-agent; retry with acknowledge_running_work=true", "work"],
    ["session_busy", "The running turn did not stop in time", null],
    ["invalid_input", "stop_running", null],
  ])("%s / %s -> %s", (code, message, expected) => {
    expect(switchRefusalOf({ code, message })).toBe(expected);
  });

  it("ignores non-errors", () => {
    expect(switchRefusalOf(undefined)).toBeNull();
  });
});

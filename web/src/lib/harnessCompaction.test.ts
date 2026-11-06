import { describe, expect, it } from "vitest";

import { compactionModeFor, compactionModesFromCatalog } from "@/lib/harnessCompaction";

describe("compactionModesFromCatalog", () => {
  it("keeps declared modes and skips rows from older servers", () => {
    const modes = compactionModesFromCatalog({
      data: [
        { id: "goose", capabilities: { compaction_mode: "reset_replay" } },
        { id: "kimi", capabilities: { compaction_mode: "none" } },
        { id: "older", capabilities: {} },
        { id: "unknown-value", capabilities: { compaction_mode: "magic" } },
        { capabilities: { compaction_mode: "native" } },
      ],
    });

    expect(modes).toEqual({ goose: "reset_replay", kimi: "none" });
  });

  it("gives a configured ACP agent's mode to the base harness", () => {
    const modes = compactionModesFromCatalog({
      data: [{ id: "acp:grok", capabilities: { compaction_mode: "reset_replay" } }],
    });

    expect(modes).toEqual({ "acp:grok": "reset_replay", acp: "reset_replay" });
  });

  it("treats a missing data list as an empty catalog", () => {
    expect(compactionModesFromCatalog({})).toEqual({});
  });
});

describe("compactionModeFor", () => {
  const modes = { acp: "reset_replay", kimi: "none" } as const;

  it("maps a namespaced ACP harness to the base row", () => {
    expect(compactionModeFor(modes, "acp:grok")).toBe("reset_replay");
  });

  it("returns undefined for an unlisted or unset harness", () => {
    expect(compactionModeFor(modes, "brand-new")).toBeUndefined();
    expect(compactionModeFor(modes, null)).toBeUndefined();
  });
});

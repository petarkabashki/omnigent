import { describe, expect, it } from "vitest";
import { harnessGlyph, harnessTitleClass } from "./harnessColor";

describe("harnessGlyph", () => {
  it("gives the families that sit together distinct colors and shapes", () => {
    const marks = ["Claude Code", "Codex", "grok", "Antigravity"].map(harnessGlyph);
    expect(new Set(marks.map((mark) => mark?.className)).size).toBe(marks.length);
    expect(new Set(marks.map((mark) => mark?.glyph)).size).toBe(marks.length);
    expect(marks.every((mark) => mark?.className.startsWith("text-harness-"))).toBe(true);
  });

  it("keeps a shared hue readable by using a different shape", () => {
    const pi = harnessGlyph("Pi");
    const kiro = harnessGlyph("Kiro");
    expect(pi?.className).toBe(kiro?.className);
    expect(pi?.glyph).not.toBe(kiro?.glyph);
  });

  it("keeps a name on the same mark and leaves an unnamed row unmarked", () => {
    expect(harnessGlyph("grok")).toEqual(harnessGlyph("grok"));
    expect(harnessGlyph("jcode")).toEqual(harnessGlyph("jcode"));
    expect(harnessTitleClass("grok")).toBe(harnessGlyph("grok")?.className);
    expect(harnessGlyph("Other")).toBeNull();
    expect(harnessGlyph("")).toBeNull();
  });
});

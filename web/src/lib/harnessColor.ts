// A colored character in front of a sidebar title, one stable pair per harness
// family. Families that share a hue still get different shapes. The name stays
// in the tooltip. Unknown agent names share the eight hues and a small set of
// fallback shapes, hashed so a given name keeps both.

const HARNESS_TITLE_CLASSES = [
  "text-harness-1",
  "text-harness-2",
  "text-harness-3",
  "text-harness-4",
  "text-harness-5",
  "text-harness-6",
  "text-harness-7",
  "text-harness-8",
] as const;

/** Shapes for agent names that are not in the named table. */
const FALLBACK_HARNESS_GLYPHS = ["•", "◉", "◐", "◑", "♦", "☀", "✱", "✚"] as const;

/** Display names from `getConversationAgentType`. Color index is 1-based. */
const NAMED_HARNESS_MARK: Record<string, { color: number; glyph: string }> = {
  "Claude Code": { color: 1, glyph: "✶" },
  Codex: { color: 2, glyph: "▣" },
  grok: { color: 3, glyph: "◈" },
  Pi: { color: 4, glyph: "π" },
  Kiro: { color: 4, glyph: "⬡" },
  Cursor: { color: 5, glyph: "▸" },
  Goose: { color: 5, glyph: "▴" },
  Antigravity: { color: 6, glyph: "✦" },
  Kimi: { color: 6, glyph: "☽" },
  Devin: { color: 7, glyph: "◆" },
  "Qwen Code": { color: 7, glyph: "※" },
  OpenCode: { color: 8, glyph: "○" },
  Hermes: { color: 8, glyph: "✧" },
};

export interface HarnessGlyph {
  glyph: string;
  className: (typeof HARNESS_TITLE_CLASSES)[number];
}

function harnessHash(agentType: string): number {
  let hash = 0;
  for (let i = 0; i < agentType.length; i++) hash = (hash * 33 + agentType.charCodeAt(i)) >>> 0;
  return hash;
}

/** Colored character for this harness label, or null when the row stays unmarked. */
export function harnessGlyph(agentType: string): HarnessGlyph | null {
  if (agentType === "" || agentType === "Other") return null;
  const named = NAMED_HARNESS_MARK[agentType];
  if (named !== undefined) {
    return { glyph: named.glyph, className: HARNESS_TITLE_CLASSES[named.color - 1]! };
  }
  const hash = harnessHash(agentType);
  return {
    glyph: FALLBACK_HARNESS_GLYPHS[hash % FALLBACK_HARNESS_GLYPHS.length]!,
    className: HARNESS_TITLE_CLASSES[hash % HARNESS_TITLE_CLASSES.length]!,
  };
}

/** Tailwind text class for this harness label, or null when the row stays unmarked. */
export function harnessTitleClass(
  agentType: string,
): (typeof HARNESS_TITLE_CLASSES)[number] | null {
  return harnessGlyph(agentType)?.className ?? null;
}

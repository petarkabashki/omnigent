import { describe, expect, it } from "vitest";
import {
  EMPTY_VISIT_HISTORY,
  isSessionPath,
  recordVisit,
  relabelCurrentVisit,
  visitLabel,
} from "./visitHistory";

const visit = (key: string) => ({ key, path: `/c/${key}`, label: key });
const keys = (state: { entries: ({ key: string } | null)[] }) =>
  state.entries.map((entry) => entry?.key ?? null);

describe("recordVisit", () => {
  const threeVisits = [visit("a"), visit("b"), visit("c")].reduce(
    (state, entry, index) => recordVisit(state, index, entry),
    EMPTY_VISIT_HISTORY,
  );

  it("appends pushes", () => {
    expect(keys(threeVisits)).toEqual(["a", "b", "c"]);
    expect(threeVisits.cursor).toBe(2);
  });

  it("keeps later entries when moving back and forward", () => {
    const back = recordVisit(threeVisits, 0, visit("a"));
    expect(keys(back)).toEqual(["a", "b", "c"]);
    expect(back.cursor).toBe(0);
    const forward = recordVisit(back, 1, visit("b"));
    expect(keys(forward)).toEqual(["a", "b", "c"]);
    expect(forward.cursor).toBe(1);
  });

  it("drops the forward entries on a new visit after going back", () => {
    const back = recordVisit(threeVisits, 0, visit("a"));
    expect(keys(recordVisit(back, 1, visit("d")))).toEqual(["a", "d"]);
  });

  it("swaps the current entry on a replace", () => {
    const back = recordVisit(threeVisits, 1, visit("b"));
    const replaced = recordVisit(back, 1, visit("r"));
    expect(keys(replaced)).toEqual(["a", "r", "c"]);
    expect(replaced.cursor).toBe(1);
  });

  it("starts over when a full page load restarts the index", () => {
    const reloaded = recordVisit(threeVisits, 0, visit("fresh"));
    expect(keys(reloaded)).toEqual(["fresh"]);
    expect(reloaded.cursor).toBe(0);
  });

  it("tells two first loads apart though they share a key", () => {
    const first = recordVisit(EMPTY_VISIT_HISTORY, 0, { ...visit("a"), key: "default" });
    const second = recordVisit(first, 1, visit("b"));
    const reloaded = recordVisit(second, 0, { key: "default", path: "/inbox", label: "Inbox" });
    expect(reloaded.entries).toHaveLength(1);
  });

  it("leaves gaps for visits from before this record began", () => {
    expect(keys(recordVisit(EMPTY_VISIT_HISTORY, 2, visit("a")))).toEqual([null, null, "a"]);
  });
});

describe("labels", () => {
  it("names sessions from the page title and other pages from their path", () => {
    expect(visitLabel("/c/abc", "● Coordinator")).toBe("Coordinator");
    expect(visitLabel("/canvas/c/abc?canvas=x", "")).toBe("Session");
    expect(visitLabel("/inbox", "Coordinator")).toBe("Inbox");
    expect(visitLabel("/", "Coordinator")).toBe("New session");
  });

  it("recognises session paths", () => {
    expect(isSessionPath("/c/abc")).toBe(true);
    expect(isSessionPath("/tasks")).toBe(false);
  });

  it("relabels only the current entry", () => {
    const state = recordVisit(recordVisit(EMPTY_VISIT_HISTORY, 0, visit("a")), 1, visit("b"));
    const renamed = relabelCurrentVisit(state, "Renamed");
    expect(renamed.entries.map((entry) => entry?.label)).toEqual(["a", "Renamed"]);
    expect(relabelCurrentVisit(renamed, "Renamed")).toBe(renamed);
  });
});

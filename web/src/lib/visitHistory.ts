// A record of this tab's browser history, which the History API won't expose.
// Entries are indexed by React Router's `history.state.idx`, so comparing a
// visit's idx and key with the cursor tells a push, a replace and a pop apart.

const STORAGE_KEY = "omnigent.visit-history";

export interface VisitEntry {
  key: string;
  path: string;
  label: string;
}

export interface VisitHistoryState {
  entries: (VisitEntry | null)[];
  cursor: number;
}

export const EMPTY_VISIT_HISTORY: VisitHistoryState = { entries: [], cursor: -1 };

export function recordVisit(
  state: VisitHistoryState,
  index: number,
  entry: VisitEntry,
): VisitHistoryState {
  // A full page load restarts the router's idx, leaving the old entries unreachable.
  // Every first load shares one key, so the path must match too.
  const known = state.entries[index];
  const isKnown = known?.key === entry.key && known.path === entry.path;
  if (index === 0 && state.cursor > 0 && !isKnown) return { entries: [entry], cursor: 0 };
  const isPush = index > state.cursor && !isKnown;
  const entries = isPush ? state.entries.slice(0, index) : [...state.entries];
  while (entries.length < index) entries.push(null);
  entries[index] = entry;
  return { entries, cursor: index };
}

export function relabelCurrentVisit(state: VisitHistoryState, label: string): VisitHistoryState {
  const current = state.entries[state.cursor];
  if (!current || current.label === label) return state;
  const entries = [...state.entries];
  entries[state.cursor] = { ...current, label };
  return { ...state, entries };
}

export function isSessionPath(path: string): boolean {
  return /(^|\/)c\/[^/?#]+/.test(path);
}

export function visitLabel(path: string, documentTitle: string): string {
  if (isSessionPath(path)) return documentTitle.replace(/^● /, "") || "Session";
  const section = path.split(/[?#]/)[0]?.split("/").find(Boolean);
  if (!section) return "New session";
  return section.charAt(0).toUpperCase() + section.slice(1);
}

export function loadVisitHistory(): VisitHistoryState {
  try {
    const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "null") as unknown;
    const candidate = stored as VisitHistoryState | null;
    if (Array.isArray(candidate?.entries) && typeof candidate?.cursor === "number") {
      return candidate;
    }
  } catch {
    // Unreadable storage only costs the forward list after a reload.
  }
  return EMPTY_VISIT_HISTORY;
}

export function saveVisitHistory(state: VisitHistoryState): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Blocked storage only costs the forward list after a reload.
  }
}

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { isMacPlatform } from "@/lib/hotkeys";
import { isElectronShell } from "@/lib/nativeBridge";
import { useLocation, useNavigate } from "@/lib/routing";
import {
  isSessionPath,
  loadVisitHistory,
  recordVisit,
  relabelCurrentVisit,
  saveVisitHistory,
  visitLabel,
  type VisitHistoryState,
} from "@/lib/visitHistory";

let visitHistory = loadVisitHistory();
const listeners = new Set<() => void>();

function publish(next: VisitHistoryState): void {
  if (next === visitHistory) return;
  visitHistory = next;
  saveVisitHistory(next);
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Replace the tab's record; for tests. */
export function setVisitHistory(state: VisitHistoryState): void {
  visitHistory = state;
  for (const listener of listeners) listener();
}

export interface VisitHistoryControls extends VisitHistoryState {
  canGoBack: boolean;
  canGoForward: boolean;
  /** Move through history by `delta` entries, negative for back. */
  go: (delta: number) => void;
}

export function useVisitHistory(): VisitHistoryControls {
  const state = useSyncExternalStore(subscribe, () => visitHistory);
  const navigate = useNavigate();
  const go = useCallback((delta: number) => void navigate(delta), [navigate]);
  return {
    ...state,
    canGoBack: state.cursor > 0,
    canGoForward: state.cursor >= 0 && state.cursor < state.entries.length - 1,
    go,
  };
}

function isTextField(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest("input, textarea, [contenteditable]") !== null;
}

/** Alt+Left and Alt+Right, except where the key already moves a cursor. */
export function historyHotkeyDelta(event: globalThis.KeyboardEvent, isMac: boolean): number | null {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  const delta = event.code === "ArrowLeft" ? -1 : event.code === "ArrowRight" ? 1 : null;
  if (delta === null || event.repeat || event.isComposing) return null;
  if (event.target instanceof Element && event.target.closest(".monaco-editor, .xterm")) {
    return null;
  }
  return isMac && isTextField(event.target) ? null : delta;
}

/**
 * Keep the tab's visit record current and bind its keys and, in the desktop app,
 * the mouse's side buttons. Bound once at the app shell.
 */
export function useVisitHistoryTracker(enabled: boolean): void {
  const { pathname, search, key } = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const index = (window.history.state as { idx?: unknown } | null)?.idx;
    if (!enabled || typeof index !== "number") return;
    const path = `${pathname}${search}`;
    publish(
      recordVisit(visitHistory, index, { key, path, label: visitLabel(path, document.title) }),
    );
    if (!isSessionPath(path)) return;
    const titleElement = document.querySelector("title");
    if (!titleElement) return;
    const observer = new MutationObserver(() => {
      publish(relabelCurrentVisit(visitHistory, visitLabel(path, document.title)));
    });
    observer.observe(titleElement, { childList: true, characterData: true, subtree: true });
    return () => observer.disconnect();
  }, [enabled, pathname, search, key]);

  useEffect(() => {
    if (!enabled) return;
    const move = (delta: number, event: globalThis.UIEvent) => {
      const target = visitHistory.cursor + delta;
      if (target < 0 || target >= visitHistory.entries.length) return;
      event.preventDefault();
      event.stopPropagation();
      void navigate(delta);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const delta = historyHotkeyDelta(event, isMacPlatform());
      if (delta !== null) move(delta, event);
    };
    const onMouseUp = (event: globalThis.MouseEvent) => {
      if (event.button === 3) move(-1, event);
      if (event.button === 4) move(1, event);
    };
    const bindSideButtons = isElectronShell();
    window.addEventListener("keydown", onKeyDown, true);
    if (bindSideButtons) window.addEventListener("mouseup", onMouseUp, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      if (bindSideButtons) window.removeEventListener("mouseup", onMouseUp, true);
    };
  }, [enabled, navigate]);
}

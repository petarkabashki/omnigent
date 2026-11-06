import { useEffect, useRef } from "react";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

const OPEN_SESSION_GRAPH_EVENT = "omnigent:open-session-graph";

/** Ask whichever shell surface owns the session graph to show it. */
export function openSessionGraph(): void {
  window.dispatchEvent(new Event(OPEN_SESSION_GRAPH_EVENT));
}

export function isSessionGraphHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  return (
    hasCommandModifier(event, isMac) && event.shiftKey && !event.altKey && event.code === "KeyG"
  );
}

/**
 * Call `onOpen` for ⌘⇧G / Ctrl+Shift+G and for `openSessionGraph()`. Captured at
 * the window so the chord still works while a terminal has focus; a code editor
 * keeps it for its own find-previous.
 */
export function useSessionGraphTrigger(onOpen: () => void): void {
  const latest = useRef(onOpen);
  latest.current = onOpen;

  useEffect(() => {
    const open = () => latest.current();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.repeat || !isSessionGraphHotkey(event)) return;
      if (event.target instanceof Element && event.target.closest(".monaco-editor")) return;
      event.preventDefault();
      event.stopPropagation();
      open();
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener(OPEN_SESSION_GRAPH_EVENT, open);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener(OPEN_SESSION_GRAPH_EVENT, open);
    };
  }, []);
}

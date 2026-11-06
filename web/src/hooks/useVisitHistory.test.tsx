import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { useLocation } from "@/lib/routing";
import { EMPTY_VISIT_HISTORY, type VisitEntry } from "@/lib/visitHistory";
import {
  historyHotkeyDelta,
  setVisitHistory,
  useVisitHistory,
  useVisitHistoryTracker,
} from "./useVisitHistory";

const entry = (key: string): VisitEntry => ({ key, path: `/c/${key}`, label: key });
const altKey = (init: KeyboardEventInit) => new KeyboardEvent("keydown", { altKey: true, ...init });

afterEach(() => {
  act(() => setVisitHistory(EMPTY_VISIT_HISTORY));
  window.history.replaceState(null, "");
});

describe("historyHotkeyDelta", () => {
  it("maps Alt+arrows to a direction", () => {
    expect(historyHotkeyDelta(altKey({ code: "ArrowLeft" }), false)).toBe(-1);
    expect(historyHotkeyDelta(altKey({ code: "ArrowRight" }), false)).toBe(1);
    expect(historyHotkeyDelta(altKey({ code: "ArrowLeft", shiftKey: true }), false)).toBeNull();
    expect(historyHotkeyDelta(altKey({ code: "KeyA" }), false)).toBeNull();
  });

  it("leaves the keys to terminals, editors and Mac text fields", () => {
    const press = (element: Element, isMac: boolean) => {
      document.body.append(element);
      let delta: number | null = null;
      element.addEventListener("keydown", (event) => {
        delta = historyHotkeyDelta(event as KeyboardEvent, isMac);
      });
      element.dispatchEvent(altKey({ code: "ArrowLeft", bubbles: true }));
      element.remove();
      return delta;
    };
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    expect(press(terminal, false)).toBeNull();
    expect(press(document.createElement("textarea"), true)).toBeNull();
    expect(press(document.createElement("textarea"), false)).toBe(-1);
  });
});

describe("useVisitHistoryTracker", () => {
  function Tracker() {
    useVisitHistoryTracker(true);
    return <span data-testid="path">{useLocation().pathname}</span>;
  }

  it("records the location and moves through history on the keys", () => {
    window.history.replaceState({ idx: 1 }, "");
    act(() => setVisitHistory({ entries: [entry("a"), null], cursor: 0 }));
    render(
      <MemoryRouter initialEntries={["/c/one", "/c/two"]} initialIndex={1}>
        <Tracker />
      </MemoryRouter>,
    );
    fireEvent.keyDown(window, { code: "ArrowLeft", altKey: true });
    expect(screen.getByTestId("path").textContent).toBe("/c/one");
  });

  it("does nothing past the end of history", () => {
    window.history.replaceState({ idx: 0 }, "");
    render(
      <MemoryRouter initialEntries={["/c/one"]}>
        <Tracker />
      </MemoryRouter>,
    );
    const event = altKey({ code: "ArrowLeft", cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(screen.getByTestId("path").textContent).toBe("/c/one");
  });
});

describe("useVisitHistory", () => {
  it("reports where the tab can go and navigates by delta", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={["/a", "/b"]} initialIndex={1}>
        {children}
      </MemoryRouter>
    );
    const { result } = renderHook(() => ({ history: useVisitHistory(), location: useLocation() }), {
      wrapper,
    });
    expect(result.current.history.canGoBack).toBe(false);
    act(() => setVisitHistory({ entries: [entry("a"), entry("b"), entry("c")], cursor: 1 }));
    expect(result.current.history.canGoBack).toBe(true);
    expect(result.current.history.canGoForward).toBe(true);
    act(() => result.current.history.go(-1));
    expect(result.current.location.pathname).toBe("/a");
  });
});

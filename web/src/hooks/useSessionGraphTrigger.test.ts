import { fireEvent, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  isSessionGraphHotkey,
  openSessionGraph,
  useSessionGraphTrigger,
} from "./useSessionGraphTrigger";

const chord = { code: "KeyG", ctrlKey: true, shiftKey: true };

describe("useSessionGraphTrigger", () => {
  it("opens on the chord and on request", () => {
    const onOpen = vi.fn();
    renderHook(() => useSessionGraphTrigger(onOpen));
    fireEvent.keyDown(document.body, chord);
    openSessionGraph();
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("leaves the chord to a code editor", () => {
    const onOpen = vi.fn();
    renderHook(() => useSessionGraphTrigger(onOpen));
    const editor = document.createElement("div");
    editor.className = "monaco-editor";
    document.body.append(editor);
    fireEvent.keyDown(editor, chord);
    expect(onOpen).not.toHaveBeenCalled();
    editor.remove();
  });

  it("needs shift and the platform modifier", () => {
    const event = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);
    expect(isSessionGraphHotkey(event({ code: "KeyG", ctrlKey: true }), false)).toBe(false);
    expect(isSessionGraphHotkey(event({ code: "KeyG", metaKey: true, shiftKey: true }), true)).toBe(
      true,
    );
    expect(
      isSessionGraphHotkey(event({ code: "KeyG", ctrlKey: true, shiftKey: true }), false),
    ).toBe(true);
  });
});

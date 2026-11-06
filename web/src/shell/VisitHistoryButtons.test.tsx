import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { setVisitHistory } from "@/hooks/useVisitHistory";
import { useLocation } from "@/lib/routing";
import { EMPTY_VISIT_HISTORY, type VisitEntry } from "@/lib/visitHistory";
import { VisitHistoryButtons } from "./VisitHistoryButtons";

const entry = (label: string): VisitEntry => ({ key: label, path: `/c/${label}`, label });

function Where() {
  return <span data-testid="path">{useLocation().pathname}</span>;
}

function renderButtons() {
  render(
    <TooltipProvider>
      <MemoryRouter initialEntries={["/one", "/two", "/three"]} initialIndex={1}>
        <VisitHistoryButtons />
        <Where />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

afterEach(() => {
  cleanup();
  act(() => setVisitHistory(EMPTY_VISIT_HISTORY));
});

describe("VisitHistoryButtons", () => {
  it("stays hidden until the tab has a record", () => {
    renderButtons();
    expect(screen.queryByTestId("visit-history-buttons")).toBeNull();
  });

  it("disables a direction with nowhere to go", () => {
    setVisitHistory({ entries: [entry("Coordinator")], cursor: 0 });
    renderButtons();
    expect(screen.getByTestId("history-back")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("history-forward")).toHaveProperty("disabled", true);
  });

  it("moves back and forward", () => {
    setVisitHistory({ entries: [entry("a"), entry("b"), entry("c")], cursor: 1 });
    renderButtons();
    fireEvent.click(screen.getByTestId("history-back"));
    expect(screen.getByTestId("path").textContent).toBe("/one");
  });

  it("jumps to a listed visit", async () => {
    setVisitHistory({ entries: [entry("Inbox"), entry("Coordinator"), entry("Lane")], cursor: 1 });
    renderButtons();
    fireEvent.pointerDown(screen.getByTestId("history-list"), { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Lane" }));
    expect(screen.getByTestId("path").textContent).toBe("/three");
  });
});

import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { WaitingPage } from "@/pages/WaitingPage";
import type { SessionWaiting, WaitingItem } from "@/lib/waitingApi";
import { WaitingItemRow } from "./WaitingItemRow";
import { WaitingStatusChip, waitingChipLabel } from "./WaitingStatusChip";

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  nudge: vi.fn(),
  stop: vi.fn(),
  fetchSession: vi.fn(),
  fetchAll: vi.fn(),
}));

vi.mock("@/lib/routing", () => ({ useNavigate: () => mocks.navigate }));
vi.mock("@/lib/roleThreads", () => ({ nudgeRoleThread: mocks.nudge }));
vi.mock("@/lib/sessionsApi", () => ({ stopSession: mocks.stop }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/waitingApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/waitingApi")>()),
  fetchSessionWaiting: mocks.fetchSession,
  fetchAllWaiting: mocks.fetchAll,
}));

afterEach(() => {
  cleanup();
  Object.values(mocks).forEach((m) => m.mockReset());
});

function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

const nowSec = Math.floor(Date.now() / 1000);

function item(overrides: Partial<WaitingItem> = {}): WaitingItem {
  return {
    kind: "tool_call",
    session_id: "s1",
    title: "Bash: pytest -q",
    since: nowSec - 120,
    stale: false,
    actions: ["open", "stop"],
    ...overrides,
  };
}

describe("WaitingItemRow", () => {
  const handlers = { onOpen: vi.fn(), onNudge: vi.fn(), onStop: vi.fn() };

  it("shows only the actions the item allows", () => {
    render(<WaitingItemRow item={item()} {...handlers} />);
    expect(screen.getByText("Open")).toBeTruthy();
    expect(screen.getByText("Stop")).toBeTruthy();
    expect(screen.queryByText("Nudge")).toBeNull();
  });

  it("opens the child session of a sub-agent and nudges a thread", () => {
    const onOpen = vi.fn();
    const onNudge = vi.fn();
    const sub = item({ kind: "subagent", ref: "child1", actions: ["open"] });
    const { unmount } = render(<WaitingItemRow item={sub} {...handlers} onOpen={onOpen} />);
    fireEvent.click(screen.getByText("Open"));
    expect(onOpen).toHaveBeenCalledWith("child1");
    unmount();
    const thread = item({ kind: "thread", ref: "proofloom/Architect", actions: ["nudge"] });
    render(<WaitingItemRow item={thread} {...handlers} onNudge={onNudge} />);
    fireEvent.click(screen.getByText("Nudge"));
    expect(onNudge).toHaveBeenCalledWith(thread);
  });

  it("marks stale items", () => {
    render(<WaitingItemRow item={item({ stale: true })} {...handlers} />);
    expect(screen.getByTestId("waiting-item").dataset.stale).toBe("true");
    expect(screen.getByTestId("waiting-item-age").textContent).toBe("2 min");
  });
});

describe("WaitingStatusChip", () => {
  it("labels running and waiting", () => {
    expect(waitingChipLabel("running", 1000 - 720, null, 1000)).toBe("Running 12 min");
    expect(waitingChipLabel("waiting", 1000 - 720, "3 background shells", 1000)).toBe(
      "Waiting 12 min · 3 background shells",
    );
    // The snapshot reports a parked session as running, with a reason.
    expect(waitingChipLabel("running", 1000 - 60, "1 approval", 1000)).toBe(
      "Waiting 1 min · 1 approval",
    );
  });

  it("renders nothing for idle sessions", () => {
    renderWithQuery(<WaitingStatusChip sessionId="s1" status="idle" statusSince={nowSec} />);
    expect(screen.queryByTestId("waiting-status-chip")).toBeNull();
  });

  it("shows age and reason, and lists items when opened", async () => {
    mocks.fetchSession.mockResolvedValue({
      session_id: "s1",
      items: [item({ title: "Bash: pytest -q" })],
    });
    renderWithQuery(
      <WaitingStatusChip
        sessionId="s1"
        status="waiting"
        statusSince={nowSec - 720}
        waitingReason="3 background shells"
      />,
    );
    const chip = screen.getByTestId("waiting-status-chip");
    expect(chip.textContent).toBe("Waiting 12 min · 3 background shells");
    fireEvent.click(chip);
    expect(await screen.findByText("Bash: pytest -q")).toBeTruthy();
    expect(mocks.fetchSession).toHaveBeenCalledWith("s1");
  });
});

describe("WaitingPage", () => {
  function session(id: string, since: number, items: WaitingItem[]): SessionWaiting {
    return {
      session_id: id,
      title: `Session ${id}`,
      status: "waiting",
      status_since: since,
      waiting_reason: null,
      items,
    };
  }

  it("orders sessions oldest first, counts stale items and nudges through", async () => {
    mocks.nudge.mockResolvedValue({});
    const thread = item({
      kind: "thread",
      session_id: "new",
      ref: "proofloom/Architect",
      since: nowSec - 100,
      stale: true,
      actions: ["nudge"],
    });
    mocks.fetchAll.mockResolvedValue([
      session("new", nowSec - 100, [thread]),
      session("old", nowSec - 5000, []),
    ]);
    renderWithQuery(<WaitingPage />);
    await waitFor(() => expect(screen.getAllByTestId("waiting-session")).toHaveLength(2));
    const titles = screen.getAllByTestId("waiting-session").map((el) => el.textContent ?? "");
    expect(titles[0]).toContain("Session old");
    expect(titles[1]).toContain("Session new");
    expect(screen.getByTestId("waiting-stale-count").textContent).toBe("1 stale item.");
    fireEvent.click(screen.getByText("Nudge"));
    await waitFor(() => expect(mocks.nudge).toHaveBeenCalledTimes(1));
    expect(mocks.nudge.mock.calls[0][0]).toBe("new");
    expect(mocks.nudge.mock.calls[0][1].counterpart).toBe("proofloom/Architect");
  });
});

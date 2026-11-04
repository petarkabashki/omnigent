import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { SubagentsGraphView } from "./SubagentsGraphView";

vi.mock("@xyflow/react", () => ({
  ReactFlow: () => <div />,
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const child: ChildSessionInfo = {
  id: "conv_child",
  title: "researcher:auth",
  task_summary: null,
  tool: "researcher",
  session_name: "auth",
  current_task_status: null,
  busy: false,
  last_message_preview: null,
  pending_elicitations_count: 0,
};
// Stable arrays and callbacks: the view compares them by identity across renders.
const rootChildren = [child];
const noChildren: ChildSessionInfo[] = [];
const withId = (id: string): ChildSessionInfo => ({ ...child, id });
// Extra levels for the depth-cap tests; empty by default.
let deeperLevels: Record<string, ChildSessionInfo[]> = {};
const loadMoreByLevel: Record<string, () => Promise<void>> = {
  conv_root: vi.fn(async () => {}),
  conv_child: vi.fn(async () => {}),
};
// Stable, as the real hook returns: a fresh function each render would loop the view.
const noopLoadMore = vi.fn(async () => {});
let olderLevels: string[] = [];
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: (id: string | null) => ({
    children: (id !== null && deeperLevels[id]) || (id === "conv_root" ? rootChildren : noChildren),
    isLoading: false,
    error: null,
    hasMore: id !== null && olderLevels.includes(id),
    loadMore: (id !== null && loadMoreByLevel[id]) || noopLoadMore,
    isLoadingMore: false,
    loadMoreError: null,
  }),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));

afterEach(() => {
  cleanup();
  olderLevels = [];
  deeperLevels = {};
  vi.clearAllMocks();
});

function renderGraph() {
  render(
    <MemoryRouter initialEntries={["/c/conv_root"]}>
      <SubagentsGraphView conversationId="conv_root" rootSessionId="conv_root" />
    </MemoryRouter>,
  );
}

describe("SubagentsGraphView depth cap", () => {
  it("says deeper agents are not shown when the tree reaches the cap", () => {
    deeperLevels = {
      conv_child: [withId("conv_g1")],
      conv_g1: [withId("conv_g2")],
    };
    renderGraph();
    expect(screen.getByTestId("graph-depth-notice").textContent).toMatch(/not shown/i);
  });

  it("has no depth notice for a shallower tree", () => {
    deeperLevels = { conv_child: [withId("conv_g1")] };
    renderGraph();
    expect(screen.queryByTestId("graph-depth-notice")).toBeNull();
  });
});

describe("SubagentsGraphView paging", () => {
  it("says nothing is hidden when every level is fully loaded", () => {
    renderGraph();
    expect(screen.queryByTestId("graph-more-notice")).toBeNull();
  });

  it("says older agents are not drawn and loads them from the root level", () => {
    olderLevels = ["conv_root"];
    renderGraph();
    expect(screen.getByTestId("graph-more-notice").textContent).toMatch(/older agents/i);
    fireEvent.click(screen.getByTestId("graph-load-more"));
    expect(loadMoreByLevel.conv_root).toHaveBeenCalledTimes(1);
  });

  it("flags a nested level that has older agents", () => {
    olderLevels = ["conv_child"];
    renderGraph();
    expect(screen.getByTestId("graph-more-notice")).toBeTruthy();
    fireEvent.click(screen.getByTestId("graph-load-more"));
    expect(loadMoreByLevel.conv_child).toHaveBeenCalledTimes(1);
  });
});

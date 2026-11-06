import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionGraphDialog } from "./SessionGraphDialog";

interface StubNode {
  id: string;
  type: string;
  data: { label: string };
}
vi.mock("@xyflow/react", () => ({
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  ReactFlow: ({
    nodes,
    onNodeClick,
  }: {
    nodes: StubNode[];
    onNodeClick: (event: unknown, node: StubNode) => void;
  }) => (
    <div>
      {nodes.map((node) => (
        <button key={node.id} type="button" onClick={(event) => onNodeClick(event, node)}>
          {node.data.label}
        </button>
      ))}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/lib/routing", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigate,
}));

const sessions = [
  { id: "coordinator", title: "Coordinator", labels: {}, project_id: "p1" },
  { id: "elsewhere", title: "Elsewhere", labels: {}, project_id: "p2" },
];
vi.mock("@/canvas/canvasSessions", () => ({
  useCanvasSessions: () => ({ sessions, loaded: true, error: null }),
}));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [
      { id: "p1", name: "omnigent" },
      { id: "p2", name: "other" },
    ],
  }),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: [] }) }));

describe("SessionGraphDialog", () => {
  beforeEach(() => navigate.mockClear());

  it("starts on the open session's project and can widen to all of them", () => {
    render(<SessionGraphDialog open onOpenChange={vi.fn()} activeSessionId="coordinator" />);
    expect(screen.getByRole("button", { name: "Coordinator" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Elsewhere" })).toBeNull();
    fireEvent.click(screen.getByTestId("session-graph-scope"));
    expect(screen.getByRole("button", { name: "Elsewhere" })).toBeInTheDocument();
  });

  it("opens a session on click and ignores a project heading", () => {
    const onOpenChange = vi.fn();
    render(<SessionGraphDialog open onOpenChange={onOpenChange} activeSessionId="coordinator" />);
    fireEvent.click(screen.getByRole("button", { name: "omnigent" }));
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Coordinator" }));
    expect(navigate).toHaveBeenCalledWith("/c/coordinator");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

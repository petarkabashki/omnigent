import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ToolCall } from "@/lib/toolResultSummary";
import type * as viewSpec from "@/lib/viewSpec";
import { resolveView } from "@/lib/viewSpec";
import { ViewRenderBody } from "./ViewRenderBody";

vi.mock("@/lib/viewSpec", async (importOriginal) => ({
  ...(await importOriginal<typeof viewSpec>()),
  resolveView: vi.fn(),
}));

const ROWS = [
  {
    id: "s1",
    title: "Desk",
    stopped: true,
    hub_ref: { origin: "hub", kind: "session", id: "s1" },
  },
  {
    id: "s2",
    title: "Remote",
    stopped: false,
    hub_ref: { origin: "elsewhere", kind: "session", id: "s2" },
  },
];

function call(view: Record<string, unknown>): ToolCall {
  const spec = { spec: 1, title: "Mine", view };
  return {
    name: "ui_render",
    args: { spec },
    output: JSON.stringify({ spec, rows: 2 }),
    state: "output-available",
  };
}

function show(view: Record<string, unknown>) {
  render(
    <MemoryRouter>
      <ViewRenderBody call={call(view)} duration={undefined} />
    </MemoryRouter>,
  );
}

describe("ViewRenderBody", () => {
  beforeEach(() => {
    vi.mocked(resolveView).mockResolvedValue({ origin: "hub", rows: ROWS, truncated: false });
  });

  it("resolves the source and links only hub rows, with rule-gated badges", async () => {
    show({
      type: "Table",
      source: { name: "sessions" },
      columns: [
        {
          header: "Session",
          value: "$.title",
          link: "$.hub_ref",
          badges: [{ text: "stopped", when: { "==": [{ var: "stopped" }, true] } }],
        },
      ],
    });

    expect(await screen.findByRole("link", { name: "Desk" })).toHaveAttribute("href", "/c/s1");
    expect(screen.getByText("Remote").closest("a")).toBeNull();
    expect(screen.getAllByText("stopped")).toHaveLength(1);
    expect(resolveView).toHaveBeenCalledWith({ name: "sessions" }, expect.any(AbortSignal));
  });

  it("shows an unsupported box for an unknown view type", async () => {
    show({ type: "Chart", source: { name: "sessions" } });
    expect(await screen.findByText('Unsupported view type "Chart".')).toBeInTheDocument();
  });
});

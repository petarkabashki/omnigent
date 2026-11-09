import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as roleThreadsApi from "@/lib/roleThreads";
import { RoleThreadHistory } from "./RoleThreadHistory";

const { useSession } = vi.hoisted(() => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RoleThreadHistory", () => {
  it("shows the thread's messages from both sides, named by role", async () => {
    useSession.mockReturnValue({
      session: { id: "sess_product", title: "x", labels: { "omnigent.alias": "Product" } },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([
      { direction: "in", sessionId: "sess_product", itemId: "i1", createdAt: 1, text: "Plan it" },
      { direction: "out", sessionId: "sess_arch", itemId: "i2", createdAt: 2, text: "Done" },
    ]);
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <RoleThreadHistory holderId="sess_product" counterpart="/Architect" />
      </QueryClientProvider>,
    );

    const history = screen.getByTestId("role-thread-history");
    await within(history).findByText("Plan it");
    const messages = within(history).getAllByTestId("role-thread-message");
    expect(messages.map((m) => [m.dataset.direction, m.textContent?.split(" · ")[0]])).toEqual([
      ["in", "/Architect"],
      ["out", "Product"],
    ]);
    expect(roleThreadsApi.fetchRoleThreadMessages).toHaveBeenCalledWith(
      "sess_product",
      "/Architect",
    );
  });
});

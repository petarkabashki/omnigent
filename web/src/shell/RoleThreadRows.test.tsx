import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import * as roleThreadsApi from "@/lib/roleThreads";
import * as sessionsApi from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { RoleThreadRows } from "./RoleThreadRows";

function holder(labels: Record<string, string>): Conversation {
  return {
    id: "sess_product",
    object: "conversation",
    title: "Product",
    created_at: 1,
    updated_at: 1,
    labels: { "omnigent.alias": "Product", ...labels },
    archived: false,
    agent_name: "Claude",
    permission_level: null,
  } as Conversation;
}

function renderRows(conversation: Conversation) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <TooltipProvider>
          <Routes>
            <Route
              path="/"
              element={
                <ul>
                  <RoleThreadRows conversation={conversation} />
                </ul>
              }
            />
            <Route path="/c/:id" element={<p data-testid="holder-page" />} />
          </Routes>
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RoleThreadRows", () => {
  it("lists a role's threads with state and unread count", () => {
    renderRows(
      holder({
        "thread.aaaaaaaaaaaa.cp": "/Architect",
        "thread.aaaaaaaaaaaa.at": "300",
        "thread.bbbbbbbbbbbb.cp": "/Bench",
        "thread.bbbbbbbbbbbb.state": "blocked",
        "thread.bbbbbbbbbbbb.unread": "3",
        "thread.bbbbbbbbbbbb.at": "100",
      }),
    );
    const rows = screen.getAllByTestId("role-thread-row");
    expect(rows.map((row) => row.textContent)).toEqual(["/BenchBlocked3", "/Architect"]);
    expect(within(rows[0]).getByLabelText("3 unread")).toBeInTheDocument();
  });

  it("renders nothing for a session with no threads", () => {
    renderRows(holder({}));
    expect(screen.queryByTestId("role-thread-rows")).not.toBeInTheDocument();
  });

  it("shows the thread's messages from both sides, not the holder session", async () => {
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([
      { direction: "in", sessionId: "sess_product", itemId: "i1", createdAt: 1, text: "Plan it" },
      { direction: "out", sessionId: "sess_arch", itemId: "i2", createdAt: 2, text: "Done" },
    ]);
    renderRows(holder({ "thread.aaaaaaaaaaaa.cp": "/Architect" }));

    fireEvent.click(screen.getByTestId("role-thread-row"));

    const dialog = await screen.findByTestId("role-thread-dialog");
    await within(dialog).findByText("Plan it");
    const messages = within(dialog).getAllByTestId("role-thread-message");
    expect(messages.map((m) => [m.dataset.direction, m.textContent?.split(" · ")[0]])).toEqual([
      ["in", "/Architect"],
      ["out", "Product"],
    ]);
    expect(roleThreadsApi.fetchRoleThreadMessages).toHaveBeenCalledWith(
      "sess_product",
      "/Architect",
    );
    expect(screen.queryByTestId("holder-page")).not.toBeInTheDocument();
  });

  it("replies through the thread's side chat beside the holder", async () => {
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([]);
    const createSideChat = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValue({ childSessionId: "sess_thread" });
    renderRows(holder({ "thread.aaaaaaaaaaaa.cp": "/Architect" }));

    fireEvent.click(screen.getByTestId("role-thread-row"));
    fireEvent.click(await screen.findByRole("button", { name: "Reply in side chat" }));

    expect(await screen.findByTestId("holder-page")).toBeInTheDocument();
    expect(createSideChat).toHaveBeenCalledWith("sess_product", undefined, {
      thread: "/Architect",
    });
    await waitFor(() =>
      expect(useChatStore.getState().sideChatToOpen).toEqual({
        childId: "sess_thread",
        parentId: "sess_product",
      }),
    );
  });
});

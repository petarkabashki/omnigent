import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
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
  return render(
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
    </MemoryRouter>,
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

  it("opens the holder with the thread's side chat", async () => {
    const createSideChat = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValue({ childSessionId: "sess_thread" });
    renderRows(holder({ "thread.aaaaaaaaaaaa.cp": "/Architect" }));

    fireEvent.click(screen.getByTestId("role-thread-row"));

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

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import * as roleThreadsApi from "@/lib/roleThreads";
import * as rolesApi from "@/lib/rolesApi";
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
    // An open thread shows only its dot; the word stays for screen readers.
    expect(rows.map((row) => row.textContent)).toEqual(["/BenchBlocked3", "/ArchitectOpen"]);
    expect(
      rows.map((row) =>
        within(row).getByTestId("role-thread-state").firstElementChild?.getAttribute("data-tone"),
      ),
    ).toEqual(["red", "neutral"]);
    expect(within(rows[0]).getByLabelText("3 not yet handled by the role")).toBeInTheDocument();
  });

  it("renders nothing for a session with no threads", () => {
    renderRows(holder({}));
    expect(screen.queryByTestId("role-thread-rows")).not.toBeInTheDocument();
  });

  it("opens the thread's side chat beside the holder on click, without a popup", async () => {
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

  it("opens the thread ready to post from the card's Post button", async () => {
    const createSideChat = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValue({ childSessionId: "sess_thread" });
    const focusRequest = vi.spyOn(roleThreadsApi, "requestRoleThreadPostFocus");
    renderRows(holder({ "thread.aaaaaaaaaaaa.cp": "/Architect" }));

    fireEvent.focus(screen.getByTestId("role-thread-row"));
    const card = await screen.findByTestId("role-thread-card");
    fireEvent.click(within(card).getByRole("button", { name: "Post" }));

    expect(focusRequest).toHaveBeenCalled();
    expect(await screen.findByTestId("holder-page")).toBeInTheDocument();
    expect(createSideChat).toHaveBeenCalledWith("sess_product", undefined, {
      thread: "/Architect",
    });
  });

  it("nudges the holder about unhandled messages from the row's bell", async () => {
    const nudge = vi.spyOn(roleThreadsApi, "nudgeRoleThread").mockResolvedValue(undefined);
    renderRows(
      holder({
        "thread.aaaaaaaaaaaa.cp": "/Architect",
        "thread.aaaaaaaaaaaa.state": "waiting_on_us",
        "thread.aaaaaaaaaaaa.unread": "2",
        "thread.bbbbbbbbbbbb.cp": "/Bench",
      }),
    );

    expect(screen.getAllByTestId("role-thread-nudge")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Nudge about /Architect" }));

    await waitFor(() =>
      expect(nudge).toHaveBeenCalledWith(
        "sess_product",
        expect.objectContaining({ counterpart: "/Architect", unread: 2 }),
      ),
    );
  });

  it("shows a compact card with actions when a row is focused", async () => {
    vi.spyOn(rolesApi, "listNamespaceRoles").mockResolvedValue([
      {
        roleId: "r1",
        name: "Architect",
        handle: "/Architect",
        path: "",
        permanent: true,
        state: "held",
        holderSessionId: "sess_architect",
      },
    ]);
    const updateSession = vi
      .spyOn(sessionsApi, "updateSession")
      .mockResolvedValue({} as Awaited<ReturnType<typeof sessionsApi.updateSession>>);
    renderRows(
      holder({
        "thread.aaaaaaaaaaaa.cp": "/Architect",
        "thread.aaaaaaaaaaaa.state": "waiting_on_us",
        "thread.aaaaaaaaaaaa.unread": "2",
        "thread.aaaaaaaaaaaa.n": "7",
        "thread.aaaaaaaaaaaa.sum": "Asked for the schema review",
      }),
    );

    fireEvent.focus(screen.getByTestId("role-thread-row"));
    const card = await screen.findByTestId("role-thread-card");
    expect(card).toHaveTextContent("/ArchitectOur turn");
    expect(card).toHaveTextContent("2 unhandled");
    expect(within(card).getByTitle("Messages")).toHaveTextContent("7");
    expect(card).toHaveTextContent("Asked for the schema review");

    const goTo = within(card).getByRole("button", { name: "Go to" });
    await waitFor(() => expect(goTo).toBeEnabled());

    fireEvent.click(within(card).getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(updateSession).toHaveBeenCalledWith("sess_product", {
        labels: expect.objectContaining({
          "thread.aaaaaaaaaaaa.state": "done",
          "thread.aaaaaaaaaaaa.unread": "0",
        }),
        silent: true,
      }),
    );

    fireEvent.click(goTo);
    expect(await screen.findByTestId("holder-page")).toBeInTheDocument();
  });
});

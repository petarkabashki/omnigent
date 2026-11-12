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

function renderHistory() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <RoleThreadHistory holderId="sess_product" counterpart="/Architect" />
    </QueryClientProvider>,
  );
}

describe("RoleThreadHistory", () => {
  it("shows the thread's messages from both sides, named by role", async () => {
    useSession.mockReturnValue({
      session: { id: "sess_product", title: "x", labels: { "omnigent.alias": "Product" } },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([
      { direction: "in", sessionId: "sess_product", itemId: "i1", createdAt: 1, text: "Plan it" },
      { direction: "owner", sessionId: "sess_product", itemId: "i3", createdAt: 3, text: "Use v2" },
      { direction: "out", sessionId: "sess_arch", itemId: "i2", createdAt: 4, text: "Done" },
      { direction: "hub", sessionId: "sess_product", itemId: "i4", createdAt: 5, text: "Nudge" },
    ]);
    renderHistory();

    const history = screen.getByTestId("role-thread-history");
    await within(history).findByText("Plan it");
    const messages = within(history).getAllByTestId("role-thread-message");
    expect(messages.map((m) => [m.dataset.direction, m.textContent?.split(" · ")[0]])).toEqual([
      ["in", "/Architect"],
      ["owner", "You"],
      ["out", "Product"],
      ["hub", "Hub reminder"],
    ]);
    expect(roleThreadsApi.fetchRoleThreadMessages).toHaveBeenCalledWith(
      "sess_product",
      "/Architect",
    );
  });

  it("names whom each owner post reached", async () => {
    useSession.mockReturnValue({
      session: { id: "sess_product", title: "x", labels: { "omnigent.alias": "Product" } },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([
      { direction: "owner", sessionId: "s", itemId: "i1", createdAt: 1, text: "A", to: "both" },
      { direction: "owner", sessionId: "s", itemId: "i2", createdAt: 2, text: "B", to: "holder" },
      {
        direction: "owner",
        sessionId: "s",
        itemId: "i3",
        createdAt: 3,
        text: "C",
        to: "counterpart",
      },
    ]);
    renderHistory();

    const history = screen.getByTestId("role-thread-history");
    await within(history).findByText("A");
    const messages = within(history).getAllByTestId("role-thread-message");
    expect(messages.map((m) => m.textContent?.split(" · ")[1])).toEqual([
      "to the thread",
      "only to Product",
      "only to /Architect",
    ]);
  });

  it("marks messages waiting for a holder and notes dropped ones", async () => {
    useSession.mockReturnValue({
      session: {
        id: "sess_product",
        title: "x",
        labels: {
          "omnigent.alias": "Product",
          "thread.aaaaaaaaaaaa.cp": "/Architect",
          "thread.aaaaaaaaaaaa.dropped": "2",
        },
      },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([
      { direction: "out", sessionId: "s", itemId: "i1", createdAt: 1, text: "Sent" },
      {
        direction: "out",
        sessionId: "s",
        itemId: "i2",
        createdAt: 2,
        text: "Parked",
        waitingHolder: true,
      },
    ]);
    renderHistory();

    await screen.findByText("Parked");
    const badges = screen.getAllByTestId("role-thread-waiting-holder");
    expect(badges).toHaveLength(1);
    expect(badges[0].closest("[data-testid=role-thread-message]")?.textContent).toContain("Parked");
    expect(screen.getByTestId("role-thread-dropped").textContent).toContain(
      "2 messages were dropped",
    );
  });

  it("shows the loop-guard notice when the thread has a held message", async () => {
    useSession.mockReturnValue({
      session: {
        id: "sess_product",
        title: "x",
        labels: {
          "omnigent.alias": "Product",
          "thread.aaaaaaaaaaaa.cp": "/Architect",
          "thread.aaaaaaaaaaaa.held": "Held words",
        },
      },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([]);
    renderHistory();
    expect(await screen.findByText("Held words")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Release" })).toBeTruthy();
  });
});

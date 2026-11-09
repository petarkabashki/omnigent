import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    ]);
    renderHistory();

    const history = screen.getByTestId("role-thread-history");
    await within(history).findByText("Plan it");
    const messages = within(history).getAllByTestId("role-thread-message");
    expect(messages.map((m) => [m.dataset.direction, m.textContent?.split(" · ")[0]])).toEqual([
      ["in", "/Architect"],
      ["owner", "You"],
      ["out", "Product"],
    ]);
    expect(roleThreadsApi.fetchRoleThreadMessages).toHaveBeenCalledWith(
      "sess_product",
      "/Architect",
    );
  });

  it("posts the owner's words to the chosen side of the thread", async () => {
    useSession.mockReturnValue({
      session: { id: "sess_product", title: "x", labels: { "omnigent.alias": "Product" } },
    });
    vi.spyOn(roleThreadsApi, "fetchRoleThreadMessages").mockResolvedValue([]);
    const post = vi
      .spyOn(roleThreadsApi, "postRoleThreadMessage")
      .mockResolvedValue({ messageId: "msg_1", delivered: ["sess_arch"] });
    renderHistory();

    const box = screen.getByTestId("role-thread-post");
    const postButton = within(box).getByRole("button", { name: "Post" });
    expect(postButton).toBeDisabled();
    fireEvent.click(within(box).getByRole("radio", { name: "/Architect" }));
    fireEvent.change(within(box).getByLabelText("Post to the thread"), {
      target: { value: "  Answer the schema question  " },
    });
    fireEvent.click(postButton);

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "sess_product",
        "/Architect",
        "Answer the schema question",
        "counterpart",
      ),
    );
    await waitFor(() => expect(within(box).getByLabelText("Post to the thread")).toHaveValue(""));
  });
});

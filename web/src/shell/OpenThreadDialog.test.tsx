import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as rolesApi from "@/lib/rolesApi";
import * as sessionsApi from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { OpenThreadDialog } from "./OpenThreadDialog";

function role(name: string, holderSessionId: string | null): rolesApi.NamespaceRole {
  return {
    roleId: name,
    name,
    handle: `forthkit/${name}`,
    path: "forthkit",
    permanent: true,
    state: holderSessionId ? "held" : "vacant",
    holderSessionId,
  };
}

function renderDialog(onOpenChange = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route
            path="/"
            element={
              <OpenThreadDialog
                open
                onOpenChange={onOpenChange}
                holderId="sess_product"
                namespacePath="forthkit"
              />
            }
          />
          <Route path="/c/:id" element={<p data-testid="holder-page" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onOpenChange;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("OpenThreadDialog", () => {
  it("lists the namespace's other roles and opens the picked one as a thread", async () => {
    vi.spyOn(rolesApi, "listNamespaceRoles").mockResolvedValue([
      role("Product", "sess_product"),
      role("Architect", "sess_arch"),
    ]);
    const createSideChat = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValue({ childSessionId: "sess_thread" });
    const onOpenChange = renderDialog();

    const choices = await screen.findAllByTestId("open-thread-role");
    expect(choices.map((choice) => choice.textContent)).toEqual(["Architect"]);
    fireEvent.click(choices[0]);

    expect(await screen.findByTestId("holder-page")).toBeInTheDocument();
    expect(createSideChat).toHaveBeenCalledWith("sess_product", undefined, {
      thread: "forthkit/Architect",
    });
    await waitFor(() =>
      expect(useChatStore.getState().sideChatToOpen).toEqual({
        childId: "sess_thread",
        parentId: "sess_product",
      }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("falls back to a typed handle", async () => {
    vi.spyOn(rolesApi, "listNamespaceRoles").mockResolvedValue([]);
    const createSideChat = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValue({ childSessionId: "sess_thread" });
    renderDialog();

    fireEvent.change(screen.getByTestId("header-open-thread-input"), {
      target: { value: " Bench " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    await waitFor(() =>
      expect(createSideChat).toHaveBeenCalledWith("sess_product", undefined, { thread: "Bench" }),
    );
  });
});

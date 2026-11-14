import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as roleThreadsApi from "@/lib/roleThreads";
import { HeldThreadMessage } from "./HeldThreadMessage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderNotice() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <HeldThreadMessage
        holderId="sess_product"
        counterpart="/Architect"
        held={{ text: "Are we done?", to: "sess_arch", at: 1 }}
      />
    </QueryClientProvider>,
  );
}

describe("HeldThreadMessage", () => {
  it("shows the held text and the reset hint", () => {
    renderNotice();
    expect(screen.getByText(/Held by loop guard/)).toBeTruthy();
    expect(screen.getByText("Are we done?")).toBeTruthy();
    expect(screen.getByText(/10 quiet minutes/)).toBeTruthy();
  });

  it.each([
    ["Release", "release"],
    ["Allow more", "allow"],
    ["Discard", "discard"],
  ])("%s calls the API with %s and disables buttons meanwhile", async (label, action) => {
    let finish: (value: unknown) => void = () => {};
    const spy = vi.spyOn(roleThreadsApi, "resolveHeldThreadMessage").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderNotice();
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(spy).toHaveBeenCalledWith("sess_product", "/Architect", action);
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Discard" }) as HTMLButtonElement).disabled).toBe(
        true,
      ),
    );
    finish({});
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Discard" }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ForkOfSession, forkParentId } from "./ForkOfSession";

const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (id: string) => getSessionSlim(id),
}));
vi.mock("@/lib/routing", () => ({
  Link: ({ to, children, ...rest }: { to: string; children: ReactNode }) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
}));

function renderWithQuery(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("forkParentId", () => {
  it("prefers the recorded parent and falls back to older routing labels", () => {
    expect(forkParentId({ "omnigent.fork.of": "a", "omnigent.side_chat.source_id": "b" })).toBe(
      "a",
    );
    expect(forkParentId({ "omnigent.side_chat.source_id": "b" })).toBe("b");
    expect(forkParentId({ "omnigent.fork.source_id": "c" })).toBe("c");
    expect(forkParentId({ "omnigent.alias": "Product" })).toBeNull();
    expect(forkParentId(undefined)).toBeNull();
  });
});

describe("ForkOfSession", () => {
  it("links the parent and names the role it holds", async () => {
    getSessionSlim.mockResolvedValueOnce({
      title: "Harbor planning",
      labels: { "omnigent.alias": "Product" },
    });
    renderWithQuery(<ForkOfSession sessionId="parent-1" />);
    const link = await screen.findByRole("link", { name: "Harbor planning" });
    expect(link.getAttribute("href")).toBe("/c/parent-1");
    expect(screen.getByTestId("fork-of-session-role").textContent).toContain("Product");
  });

  it("omits the role when the parent holds none", async () => {
    getSessionSlim.mockResolvedValueOnce({ title: "Harbor planning", labels: {} });
    renderWithQuery(<ForkOfSession sessionId="parent-2" />);
    await screen.findByRole("link", { name: "Harbor planning" });
    expect(screen.queryByTestId("fork-of-session-role")).toBeNull();
  });

  it("does not reveal an unreadable parent", async () => {
    getSessionSlim.mockRejectedValueOnce(new Error("forbidden"));
    renderWithQuery(<ForkOfSession sessionId="parent-3" />);
    expect((await screen.findByTestId("fork-of-session")).textContent).toBe(
      "Fork of another session",
    );
    expect(screen.queryByRole("link")).toBeNull();
  });
});

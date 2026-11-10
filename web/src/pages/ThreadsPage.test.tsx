// ThreadsPage: start a thread, read a thread, post to chosen members, invite with
// a primer. The API module is mocked; its pure helpers stay real.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/groupThreads";
import { RoutingProvider, basenamedRouting } from "@/lib/routing";
import { ThreadsPage } from "./ThreadsPage";

vi.mock("@/lib/groupThreads", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listGroupThreads: vi.fn(),
  fetchGroupThread: vi.fn(),
  fetchGroupThreadPosts: vi.fn(),
  createGroupThread: vi.fn(),
  postToGroupThread: vi.fn(),
  inviteToGroupThread: vi.fn(),
  removeFromGroupThread: vi.fn(),
  updateGroupThread: vi.fn(),
}));

const SESSION = "ab".repeat(16);
const THREAD = {
  id: "t1",
  key: "grp_t1",
  title: "Release plan",
  summary: "",
  state: "open",
  lastSeq: 1,
  createdAt: 1,
  updatedAt: null,
};
const MEMBERS: api.GroupThreadMember[] = [
  {
    id: "m1",
    kind: "role",
    ref: "omnigent/Product",
    state: "held",
    sessionId: "s1",
    deliveredSeq: 1,
  },
  { id: "m2", kind: "role", ref: "/Architect", state: "vacant", sessionId: null, deliveredSeq: 0 },
  { id: "m3", kind: "session", ref: SESSION, state: "held", sessionId: SESSION, deliveredSeq: 1 },
];

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <RoutingProvider value={basenamedRouting("")}>
          <Routes>
            <Route path="/threads" element={<ThreadsPage />} />
            <Route path="/threads/:threadKey" element={<ThreadsPage />} />
          </Routes>
        </RoutingProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.listGroupThreads).mockResolvedValue([THREAD]);
  vi.mocked(api.fetchGroupThread).mockResolvedValue({ thread: THREAD, members: MEMBERS });
  vi.mocked(api.fetchGroupThreadPosts).mockResolvedValue([
    {
      id: "p1",
      seq: 1,
      authorKind: "role",
      authorRef: "omnigent/Product",
      text: "Draft is ready",
      createdAt: 1,
      mentions: [],
    },
  ]);
  vi.mocked(api.postToGroupThread).mockResolvedValue({ delivered: 1 });
  vi.mocked(api.inviteToGroupThread).mockResolvedValue(MEMBERS[0]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ThreadsPage", () => {
  it("starts a thread with role and session members and opens it", async () => {
    vi.mocked(api.createGroupThread).mockResolvedValue(THREAD);
    renderAt("/threads");
    expect(await screen.findByText("Release plan")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Release plan" } });
    fireEvent.change(screen.getByLabelText("Members"), {
      target: { value: `omnigent/Product\n\n${SESSION}` },
    });
    fireEvent.change(screen.getByLabelText("First post"), { target: { value: "Kickoff" } });
    fireEvent.click(screen.getByRole("button", { name: "Start thread" }));

    await waitFor(() =>
      expect(api.createGroupThread).toHaveBeenCalledWith({
        title: "Release plan",
        members: [
          { kind: "role", ref: "omnigent/Product" },
          { kind: "session", ref: SESSION },
        ],
        message: "Kickoff",
      }),
    );
    expect(await screen.findByText("Draft is ready")).toBeTruthy();
  });

  it("shows posts and vacant members, and posts to the chosen members only", async () => {
    renderAt("/threads/grp_t1");
    expect(await screen.findByText("Draft is ready")).toBeTruthy();
    const vacant = screen
      .getAllByTestId("group-thread-member")
      .find((chip) => chip.dataset.state === "vacant");
    expect(vacant?.textContent).toContain("/Architect");

    fireEvent.change(screen.getByLabelText("Post"), { target: { value: "Review it" } });
    const picker = screen.getByRole("group", { name: "Post to" });
    fireEvent.click(within(picker).getByRole("button", { name: "/Architect" }));
    fireEvent.click(screen.getByRole("button", { name: /Post$/ }));

    await waitFor(() =>
      expect(api.postToGroupThread).toHaveBeenCalledWith("grp_t1", "Review it", ["/Architect"]),
    );
  });

  it("posts to everyone by default and invites with the chosen primer", async () => {
    renderAt("/threads/grp_t1");
    await screen.findByText("Draft is ready");

    fireEvent.change(screen.getByLabelText("Post"), { target: { value: "All hands" } });
    fireEvent.click(screen.getByRole("button", { name: /Post$/ }));
    await waitFor(() =>
      expect(api.postToGroupThread).toHaveBeenCalledWith("grp_t1", "All hands", "everyone"),
    );

    fireEvent.change(screen.getByLabelText("Invite member"), { target: { value: "x/Bench" } });
    fireEvent.change(screen.getByLabelText("Primer"), { target: { value: "full" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() =>
      expect(api.inviteToGroupThread).toHaveBeenCalledWith(
        "grp_t1",
        { kind: "role", ref: "x/Bench" },
        "full",
      ),
    );
  });
});

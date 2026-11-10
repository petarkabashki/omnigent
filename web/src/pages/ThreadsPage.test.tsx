// ThreadsPage: start a thread, read a thread, post to chosen members, invite with
// a primer. The API module is mocked; its pure helpers stay real.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as picker from "@/components/threads/ThreadMemberPicker";
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

vi.mock("@/components/threads/ThreadMemberPicker", async (importOriginal) => ({
  ...(await importOriginal<typeof picker>()),
  useMemberOptions: () => OPTIONS,
}));

const SESSION = "ab".repeat(16);
const OPTIONS: picker.MemberOption[] = [
  { kind: "role", ref: "omnigent/Product", label: "omnigent/Product", detail: "role" },
  { kind: "role", ref: "x/Bench", label: "x/Bench", detail: "role · vacant" },
  { kind: "session", ref: SESSION, label: "Release notes", detail: "session abababab" },
];
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
  it("starts a thread with members picked by search and opens it", async () => {
    vi.mocked(api.createGroupThread).mockResolvedValue(THREAD);
    renderAt("/threads");
    expect(await screen.findByText("Release plan")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Release plan" } });
    const members = screen.getByRole("combobox", { name: "Members" });
    fireEvent.change(members, { target: { value: "prod" } });
    fireEvent.click(screen.getByRole("option", { name: /omnigent\/Product/ }));
    fireEvent.change(members, { target: { value: "release" } });
    fireEvent.keyDown(members, { key: "Enter" });
    fireEvent.change(members, { target: { value: "y/Typed" } });
    fireEvent.keyDown(members, { key: "Enter" });
    expect(screen.getAllByTestId("chosen-member").map((chip) => chip.textContent)).toEqual([
      "omnigent/Product",
      "Release notes",
      "y/Typed",
    ]);
    fireEvent.change(screen.getByLabelText("First post"), { target: { value: "Kickoff" } });
    fireEvent.click(screen.getByRole("button", { name: "Start thread" }));

    await waitFor(() =>
      expect(api.createGroupThread).toHaveBeenCalledWith({
        title: "Release plan",
        members: [
          { kind: "role", ref: "omnigent/Product" },
          { kind: "session", ref: SESSION },
          { kind: "role", ref: "y/Typed" },
        ],
        message: "Kickoff",
      }),
    );
    expect(await screen.findByText("Draft is ready")).toBeTruthy();
  });

  it("fills members from the link a role's menu opens", async () => {
    renderAt(api.newThreadPath(["omnigent/Product"]));
    expect(await screen.findByTestId("chosen-member")).toBeTruthy();
    expect(screen.getByTestId("chosen-member").textContent).toBe("omnigent/Product");
    fireEvent.change(screen.getByRole("combobox", { name: "Members" }), {
      target: { value: "omni" },
    });
    // A chosen member is not suggested again.
    expect(screen.queryByRole("option", { name: /omnigent\/Product/ })).toBeNull();
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

    fireEvent.change(screen.getByRole("combobox", { name: "Invite member" }), {
      target: { value: "bench" },
    });
    fireEvent.click(screen.getByRole("option", { name: /x\/Bench/ }));
    expect(screen.getByTestId("invite-choice").textContent).toContain("x/Bench");
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

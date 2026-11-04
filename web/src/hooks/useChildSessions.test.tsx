import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useChildSessions, useDescendantSession } from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});

describe("useChildSessions paging", () => {
  const page = (ids: string[], hasMore: boolean) =>
    new Response(
      JSON.stringify({
        object: "list",
        data: ids.map((id) => ({
          id,
          title: `researcher:${id}`,
          tool: "researcher",
          session_name: id,
          current_task_status: null,
          busy: false,
        })),
        last_id: ids[ids.length - 1],
        has_more: hasMore,
      }),
    );

  it("reports older children and appends the next page after the cursor", async () => {
    // WHY: the server caps a page; the hook must surface has_more and resume
    // from last_id so the UI can say so and fetch the rest.
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page(["conv_new"], true))
      .mockResolvedValueOnce(page(["conv_old"], false));
    const { result } = renderHook(() => useChildSessions("conv_parent"), { wrapper });

    await waitFor(() => expect(result.current.hasMore).toBe(true));
    expect(result.current.children.map((child) => child.id)).toEqual(["conv_new"]);

    await act(async () => {
      await result.current.loadMore();
    });

    expect(authenticatedFetch).toHaveBeenLastCalledWith(
      "/v1/sessions/conv_parent/child_sessions?after=conv_new",
    );
    expect(result.current.children.map((child) => child.id)).toEqual(["conv_new", "conv_old"]);
    expect(result.current.hasMore).toBe(false);
  });
});

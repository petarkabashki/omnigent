import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useChildSessions, useDescendantSession } from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

let queryClient: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

  it("keeps the loaded pages when the list is refetched", async () => {
    // WHY: any child status event invalidates the list; a refetch of page 1
    // alone would drop the older rows the user asked for and reset the cursor.
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page(["conv_new"], true))
      .mockResolvedValueOnce(page(["conv_old"], false))
      .mockResolvedValueOnce(page(["conv_new"], true))
      .mockResolvedValueOnce(page(["conv_old"], false));
    const { result } = renderHook(() => useChildSessions("conv_parent"), { wrapper });
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(async () => {
      await result.current.loadMore();
    });
    expect(result.current.children).toHaveLength(2);

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["conversation", "conv_parent"] });
    });

    expect(result.current.children.map((child) => child.id)).toEqual(["conv_new", "conv_old"]);
    expect(result.current.hasMore).toBe(false);
  });

  it("shows why Load more failed and refetches when the cursor went stale", async () => {
    // WHY: a deleted cursor row answers 400 stale_cursor; retrying the same
    // cursor can never succeed, so the list must restart and the user be told.
    const stale = () =>
      new Response(JSON.stringify({ error: { code: "stale_cursor", message: "gone" } }), {
        status: 400,
      });
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page(["conv_new"], true))
      .mockResolvedValueOnce(stale())
      .mockResolvedValueOnce(page(["conv_new2"], false));
    const { result } = renderHook(() => useChildSessions("conv_parent"), { wrapper });
    await waitFor(() => expect(result.current.hasMore).toBe(true));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.loadMoreError).toMatch(/changed/i);
    await waitFor(() =>
      expect(result.current.children.map((child) => child.id)).toEqual(["conv_new2"]),
    );
    expect(result.current.hasMore).toBe(false);
  });

  it("reports a failed Load more instead of swallowing it", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page(["conv_new"], true))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { message: "Server exploded" } }), { status: 500 }),
      );
    const { result } = renderHook(() => useChildSessions("conv_parent"), { wrapper });
    await waitFor(() => expect(result.current.hasMore).toBe(true));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.loadMoreError).toBe("Server exploded");
    expect(result.current.hasMore).toBe(true);
  });
});

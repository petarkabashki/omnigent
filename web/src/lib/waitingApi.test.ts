import { afterEach, describe, expect, it, vi } from "vitest";
import {
  countOwnerItems,
  fetchAllWaiting,
  fetchSessionWaiting,
  formatWaitAge,
  sessionContext,
  type SessionWaiting,
} from "./waitingApi";

const fetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

afterEach(() => fetchMock.mockReset());

describe("formatWaitAge", () => {
  it.each([
    [45, "45 s"],
    [12 * 60, "12 min"],
    [3 * 3600 + 5, "3 h"],
    [2 * 86400, "2 d"],
    [-5, "0 s"],
  ])("formats %i seconds as %s", (seconds, expected) => {
    expect(formatWaitAge(1000 - seconds, 1000)).toBe(expected);
  });
});

describe("fetchers", () => {
  it("fetchSessionWaiting reads the per-session route and defaults items", async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ session_id: "s 1" }) });
    const result = await fetchSessionWaiting("s 1");
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/s%201/waiting");
    expect(result.items).toEqual([]);
  });

  it("fetchAllWaiting unwraps sessions", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        sessions: [
          {
            session_id: "a",
            // A stale "waiting on them" thread never counts; only owner waits do.
            items: [
              { stale: true, needs_owner: false },
              { stale: false, needs_owner: true },
            ],
          },
        ],
      }),
    });
    const sessions = await fetchAllWaiting();
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/waiting");
    expect(countOwnerItems(sessions)).toBe(1);
  });

  it("sessionContext names the handle, a former handle, or the project", () => {
    const base = { session_id: "a", items: [] } as unknown as SessionWaiting;
    expect(sessionContext({ ...base, handle: "omnigent/Product", holds_role: true })).toBe(
      "omnigent/Product",
    );
    expect(sessionContext({ ...base, handle: "omnigent/Product", holds_role: false })).toBe(
      "formerly omnigent/Product",
    );
    expect(sessionContext({ ...base, project: "omnigent" })).toBe("omnigent");
    expect(sessionContext(base)).toBeNull();
  });

  it("throws on an error response", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "boom",
      text: async () => "boom",
      json: async () => ({}),
      headers: new Headers(),
    });
    await expect(fetchAllWaiting()).rejects.toBeTruthy();
  });
});

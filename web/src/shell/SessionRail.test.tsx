import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";

const h = vi.hoisted(() => ({
  hook: {} as Record<string, unknown>,
}));

vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: () => h.hook,
}));

import { SessionRail } from "./SessionRail";

function setHook(overrides: Record<string, unknown>) {
  h.hook = {
    children: [
      { id: "conv_c1", title: "researcher:auth", tool: "researcher", session_name: "auth" },
    ],
    hasMore: false,
    loadMore: vi.fn(() => Promise.resolve()),
    isLoadingMore: false,
    loadMoreError: null,
    ...overrides,
  };
}

afterEach(cleanup);

describe("SessionRail", () => {
  it("says older agents are not listed and loads them on request", () => {
    const loadMore = vi.fn(() => Promise.resolve());
    setHook({ hasMore: true, loadMore });
    render(
      <SessionRail conversationId="conv_main" onExpandExecutionLogs={vi.fn()} suppressed={false} />,
    );

    expect(screen.getByTestId("older-agents-notice")).toHaveTextContent("older agents not shown");
    fireEvent.click(screen.getByTestId("older-agents-load-more"));
    expect(loadMore).toHaveBeenCalledTimes(1);
  });

  it("shows no notice when every agent is listed", () => {
    setHook({});
    render(
      <SessionRail conversationId="conv_main" onExpandExecutionLogs={vi.fn()} suppressed={false} />,
    );
    expect(screen.queryByTestId("older-agents-notice")).toBeNull();
  });
});

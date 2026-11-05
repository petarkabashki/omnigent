import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SERVER_UNREACHABLE_DELAY_MS } from "@/hooks/useServerUnreachable";
import { ServerUnreachableBanner } from "./ServerUnreachableBanner";

const socket = vi.hoisted(() => ({ connected: true, everConnected: true }));
const listeners = vi.hoisted(() => new Set<() => void>());

vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: {
    isConnected: () => socket.connected,
    hasConnected: () => socket.everConnected,
    subscribeStatus: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  },
}));

function setConnected(connected: boolean) {
  act(() => {
    socket.connected = connected;
    for (const listener of listeners) listener();
  });
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe("ServerUnreachableBanner", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    socket.connected = true;
    socket.everConnected = true;
  });
  afterEach(() => {
    cleanup();
    listeners.clear();
    vi.useRealTimers();
  });

  it("shows nothing while connected", () => {
    render(<ServerUnreachableBanner />);
    advance(SERVER_UNREACHABLE_DELAY_MS * 2);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows nothing for a drop shorter than the delay", () => {
    render(<ServerUnreachableBanner />);
    setConnected(false);
    advance(SERVER_UNREACHABLE_DELAY_MS - 1);
    setConnected(true);
    advance(SERVER_UNREACHABLE_DELAY_MS * 2);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("says it cannot reach the server after the delay and clears on reconnect", () => {
    render(<ServerUnreachableBanner />);
    setConnected(false);
    advance(SERVER_UNREACHABLE_DELAY_MS);
    expect(screen.getByRole("status").textContent).toContain("Can't reach the server.");
    expect(screen.getByRole("status").textContent).toContain("keeps retrying");
    setConnected(true);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("stays quiet before the stream has ever connected", () => {
    socket.connected = false;
    socket.everConnected = false;
    render(<ServerUnreachableBanner />);
    advance(SERVER_UNREACHABLE_DELAY_MS * 2);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("reserves its height for the shell while shown and releases it on reconnect", () => {
    const root = document.documentElement;
    render(<ServerUnreachableBanner />);
    expect(root.style.getPropertyValue("--server-banner-height")).toBe("");
    setConnected(false);
    advance(SERVER_UNREACHABLE_DELAY_MS);
    expect(root.style.getPropertyValue("--server-banner-height")).not.toBe("");
    setConnected(true);
    expect(root.style.getPropertyValue("--server-banner-height")).toBe("");
  });
});

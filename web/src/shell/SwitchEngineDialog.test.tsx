import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { ApiError, switchSessionEngine } from "@/lib/sessionsApi";
import { SwitchEngineDialog, switchEngineTargets } from "./SwitchEngineDialog";

const agent = (id: string, harness: string | null): AvailableAgent =>
  ({ id, name: id, display_name: id, harness, builtin: true }) as unknown as AvailableAgent;

const AGENTS = [
  agent("ag_claude", "claude-native"),
  agent("ag_codex", "codex-native"),
  agent("ag_pi", "pi-native"),
  agent("ag_unknown", null),
];
const HOST = {
  host_id: "h1",
  configured_harnesses: { "claude-native": true, "codex-native": true, "pi-native": false },
} as unknown as Host;

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: AGENTS }),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: [HOST] }) }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  switchSessionEngine: vi.fn(),
}));

afterEach(cleanup);

function renderDialog(onOpenChange = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SwitchEngineDialog
        sessionId="conv_1"
        currentHarness="claude-native"
        hostId="h1"
        open
        onOpenChange={onOpenChange}
      />
    </QueryClientProvider>,
  );
  return onOpenChange;
}

describe("switchEngineTargets", () => {
  it("keeps installed agents on another known harness", () => {
    expect(switchEngineTargets(AGENTS, "claude-native", HOST).map((a) => a.id)).toEqual([
      "ag_codex",
    ]);
  });

  it("keeps every other harness when the host reports no readiness", () => {
    const bare = { host_id: "h2" } as unknown as Host;
    expect(switchEngineTargets(AGENTS, "claude-native", bare).map((a) => a.id)).toEqual([
      "ag_codex",
      "ag_pi",
    ]);
  });
});

describe("SwitchEngineDialog", () => {
  it("states what stays and what does not carry over", () => {
    renderDialog();
    const text = screen.getByTestId("switch-engine-dialog").textContent ?? "";
    expect(text).toMatch(/keeps its id and handle/);
    expect(text).toMatch(/handoff note, not the old engine's memory/);
  });

  it("posts the chosen agent and closes", async () => {
    vi.mocked(switchSessionEngine).mockResolvedValue({ engineChanged: true });
    const onOpenChange = renderDialog();
    expect(screen.getByTestId("switch-engine-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("switch-engine-agent-select"));
    fireEvent.click(await screen.findByTestId("switch-engine-agent-option-ag_codex"));
    fireEvent.click(screen.getByTestId("switch-engine-submit"));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(switchSessionEngine).toHaveBeenCalledWith(
      "conv_1",
      { agentId: "ag_codex" },
      expect.any(String),
      {},
    );
  });

  it("shows a refusal inline and stays open", async () => {
    vi.mocked(switchSessionEngine).mockRejectedValue(
      new ApiError("Target harness is not installed.", 400, "INVALID_INPUT"),
    );
    const onOpenChange = renderDialog();
    fireEvent.click(screen.getByTestId("switch-engine-agent-select"));
    fireEvent.click(await screen.findByTestId("switch-engine-agent-option-ag_codex"));
    fireEvent.click(screen.getByTestId("switch-engine-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Target harness is not installed.");
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("shows the placeholder before a choice", () => {
    renderDialog();
    expect(screen.getByTestId("switch-engine-agent-select")).toHaveTextContent("Choose an engine");
  });

  async function chooseAndSubmit(agentId: string) {
    fireEvent.click(screen.getByTestId("switch-engine-agent-select"));
    fireEvent.click(await screen.findByTestId(`switch-engine-agent-option-${agentId}`));
    fireEvent.click(screen.getByTestId("switch-engine-submit"));
  }

  it("asks before dropping queued input, then retries with the same key", async () => {
    vi.mocked(switchSessionEngine)
      .mockReset()
      .mockRejectedValueOnce(
        new ApiError(
          "The session has queued input; retry with pending='drop'",
          409,
          "pending_inputs",
        ),
      )
      .mockResolvedValueOnce({ engineChanged: true });
    const onOpenChange = renderDialog();
    await chooseAndSubmit("ag_codex");
    expect(await screen.findByTestId("switch-engine-confirm")).toHaveTextContent(/queued/);
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("switch-engine-confirm-submit"));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const [first, second] = vi.mocked(switchSessionEngine).mock.calls;
    expect(first[3]).toEqual({});
    expect(second[3]).toEqual({ dropPending: true });
    expect(second[2]).toBe(first[2]);
  });

  it("stacks confirmations for a running turn and live work", async () => {
    vi.mocked(switchSessionEngine)
      .mockReset()
      .mockRejectedValueOnce(
        new ApiError("The session is running; retry with stop_running=true", 409, "session_busy"),
      )
      .mockRejectedValueOnce(
        new ApiError(
          "1 running sub-agent; retry with acknowledge_running_work=true",
          409,
          "session_busy",
        ),
      )
      .mockResolvedValueOnce({ engineChanged: true });
    const onOpenChange = renderDialog();
    await chooseAndSubmit("ag_codex");
    fireEvent.click(await screen.findByTestId("switch-engine-confirm-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("switch-engine-confirm")).toHaveTextContent(/sub-agents/),
    );
    fireEvent.click(screen.getByTestId("switch-engine-confirm-submit"));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(vi.mocked(switchSessionEngine).mock.calls[2][3]).toEqual({
      stopRunning: true,
      acknowledgeRunningWork: true,
    });
  });
});

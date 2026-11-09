import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ExternalTaskPanel } from "./ExternalTaskPanel";

const useSession = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useSession", () => ({ useSession }));

describe("ExternalTaskPanel", () => {
  it("shows state, exit code, result, host and unit for an external row", () => {
    useSession.mockReturnValue({
      session: {
        labels: {
          "omnigent.external.kind": "systemd_unit",
          "omnigent.external.state": "failed",
          "omnigent.external.exit_code": "9",
          "omnigent.external.detail": "oom-kill",
          "omnigent.external.host_id": "host-1",
          "omnigent.external.unit": "fk-gate-7",
        },
      },
    });
    render(<ExternalTaskPanel conversationId="c1" />);
    for (const text of ["failed", "9", "oom-kill", "host-1", "fk-gate-7"]) {
      expect(screen.getByText(text)).toBeTruthy();
    }
  });

  it("renders nothing for an ordinary session", () => {
    useSession.mockReturnValue({ session: { labels: {} } });
    render(<ExternalTaskPanel conversationId="c1" />);
    expect(screen.queryByTestId("external-task-panel")).toBeNull();
  });
});

// MessagingRulesPage: load, validated save, and the "Test a pair" form. The
// react-query hooks are mocked so no QueryClient or network is needed.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessagingRulesPage } from "./MessagingRulesPage";
import * as hooks from "@/hooks/useMessagingRules";

vi.mock("@/hooks/useMessagingRules", () => ({
  useMessagingRules: vi.fn(),
  useSaveMessagingRules: vi.fn(),
  useTestMessagingPair: vi.fn(),
}));

const saveMutate = vi.fn();
const testMutate = vi.fn();

function mutation(mutate: typeof saveMutate, extra: object = {}) {
  return { mutate, isPending: false, isError: false, isSuccess: false, ...extra } as never;
}

beforeEach(() => {
  vi.mocked(hooks.useMessagingRules).mockReturnValue({
    data: { policy: "m", path: "/x/rules.yaml", text: "default: ask\n", valid: true, error: null },
    isLoading: false,
    error: null,
  } as never);
  vi.mocked(hooks.useSaveMessagingRules).mockReturnValue(mutation(saveMutate));
  vi.mocked(hooks.useTestMessagingPair).mockReturnValue(mutation(testMutate));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MessagingRulesPage", () => {
  it("shows the file path and text, and saves only after an edit", () => {
    render(<MessagingRulesPage />);
    expect(screen.getByText("/x/rules.yaml")).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Rules file"), { target: { value: "default: deny\n" } });
    fireEvent.click(save);
    expect(saveMutate).toHaveBeenCalledWith("default: deny\n");
  });

  it("shows the server's validation error after a rejected save", () => {
    vi.mocked(hooks.useSaveMessagingRules).mockReturnValue(
      mutation(saveMutate, { isError: true, error: new Error("rule 1: until needs a timezone") }),
    );
    render(<MessagingRulesPage />);
    expect(screen.getByRole("alert").textContent).toContain("until needs a timezone");
  });

  it("tests a pair against the editor text and shows the decision", () => {
    vi.mocked(hooks.useTestMessagingPair).mockReturnValue(
      mutation(testMutate, {
        data: { action: "ALLOW", rule_index: 1, reason: "messaging rule 1 (a/* -> b/*: allow)" },
      }),
    );
    render(<MessagingRulesPage />);
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "a/X" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "b/Y" } });
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    expect(testMutate).toHaveBeenCalledWith({ from: "a/X", to: "b/Y", text: "default: ask\n" });
    expect(screen.getByTestId("pair-result").textContent).toContain("ALLOW");
  });

  it("explains when no messaging policy is enabled", () => {
    vi.mocked(hooks.useMessagingRules).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error("No enabled messaging_rules policy"),
    } as never);
    render(<MessagingRulesPage />);
    expect(screen.getByRole("alert").textContent).toContain("No enabled messaging_rules");
  });
});

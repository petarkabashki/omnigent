import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as RolesApiModule from "@/lib/rolesApi";
import type { NamespaceRole } from "@/lib/rolesApi";
import { ApiError } from "@/lib/sessionsApi";
import { HeaderRoleHandle } from "./HeaderRoleHandle";
import { RolesList } from "./RolesList";

const api = vi.hoisted(() => ({
  listNamespaceRoles: vi.fn(),
  takeSessionRole: vi.fn(),
  releaseSessionRole: vi.fn(),
}));

vi.mock("@/lib/rolesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof RolesApiModule>();
  return { ...actual, ...api };
});

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), info: vi.fn() },
}));

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

function role(
  overrides: Partial<NamespaceRole> & Pick<NamespaceRole, "name" | "state">,
): NamespaceRole {
  return {
    roleId: overrides.roleId === undefined ? `role-${overrides.name}` : overrides.roleId,
    name: overrides.name,
    handle: overrides.handle ?? `lab/${overrides.name}`,
    path: overrides.path ?? "lab",
    permanent: overrides.permanent ?? false,
    state: overrides.state,
    holderSessionId: overrides.holderSessionId ?? null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.takeSessionRole.mockResolvedValue(undefined);
  api.releaseSessionRole.mockResolvedValue(undefined);
});

afterEach(cleanup);

function chipFor(name: string) {
  const chip = screen
    .getAllByTestId("role-chip")
    .find((candidate) => candidate.textContent?.startsWith(name));
  if (!chip) throw new Error(`no chip for ${name}`);
  return chip;
}

describe("RolesList", () => {
  it("fills a held chip, including a label with no role id, and leaves a vacant chip hollow", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({ name: "Held", state: "held", holderSessionId: "sess-other" }),
      role({ name: "Legacy", state: "held", roleId: null, holderSessionId: "sess-legacy" }),
      role({
        name: "Vacant",
        state: "vacant",
        holderSessionId: "sess-dead",
        handle: "lab/Vacant",
      }),
    ]);
    renderWithQuery(<RolesList namespacePath="lab" sessionId="sess-me" />);
    expect(await screen.findByText("lab/Held")).toBeInTheDocument();
    expect(chipFor("Held")).toHaveAttribute("data-state", "held");
    expect(chipFor("Held").className).toContain("bg-primary");
    expect(chipFor("Legacy")).toHaveAttribute("data-state", "held");
    expect(chipFor("Vacant")).toHaveAttribute("data-state", "vacant");
    expect(chipFor("Vacant").className).toContain("bg-transparent");
    expect(screen.getAllByTestId("role-row")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Take over" })).toBeNull();
    expect(screen.getByRole("button", { name: "Take" })).toBeInTheDocument();
  });

  it("claims a vacant role by its name", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({ name: "Architect", state: "vacant", handle: "lab/Architect" }),
    ]);
    renderWithQuery(<RolesList namespacePath="lab" sessionId="sess-me" />);
    fireEvent.click(await screen.findByRole("button", { name: "Take" }));
    await waitFor(() => expect(api.takeSessionRole).toHaveBeenCalledWith("sess-me", "Architect"));
    expect(JSON.stringify(api.takeSessionRole.mock.calls)).not.toContain("takeover");
    expect(screen.queryByRole("button", { name: "Take over" })).toBeNull();
  });

  it("shows the server's conflict message when the claim is refused", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({ name: "Architect", state: "vacant", handle: "lab/Architect" }),
    ]);
    api.takeSessionRole.mockRejectedValue(
      new ApiError(
        "Session sess-me already holds role 'Product'; release it first",
        409,
        "conflict",
      ),
    );
    renderWithQuery(<RolesList namespacePath="lab" sessionId="sess-me" />);
    fireEvent.click(await screen.findByRole("button", { name: "Take" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Session sess-me already holds role 'Product'; release it first",
      ),
    );
    expect(toast.info).not.toHaveBeenCalled();
  });

  it("names the unfiled root and does not offer take without a session", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({ name: "Scratch", state: "held", path: "", handle: "/Scratch", holderSessionId: "s" }),
    ]);
    renderWithQuery(<RolesList namespacePath="" />);
    expect(await screen.findByText("/ (unfiled)")).toBeInTheDocument();
    expect(screen.getByText("/Scratch")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take" })).toBeNull();
  });
});

describe("HeaderRoleHandle", () => {
  it("shows a leading slash from the alias until the list arrives", async () => {
    let resolveList: (roles: NamespaceRole[]) => void = () => {};
    api.listNamespaceRoles.mockReturnValue(
      new Promise<NamespaceRole[]>((resolve) => {
        resolveList = resolve;
      }),
    );
    renderWithQuery(<HeaderRoleHandle conversationId="s" namespacePath="" alias="Scratch" />);
    expect(await screen.findByTestId("header-role-handle")).toHaveTextContent("/Scratch");
    expect(screen.getByTestId("header-role-handle").className).not.toMatch(/\bhidden\b/);
    resolveList([]);
  });

  it("uses the listed handle once the session is confirmed as the holder", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({
        name: "Product",
        state: "held",
        path: "forthkit",
        handle: "forthkit/Product",
        holderSessionId: "sess-product",
      }),
    ]);
    renderWithQuery(
      <HeaderRoleHandle conversationId="sess-product" namespacePath="forthkit" alias="Product" />,
    );
    expect(await screen.findByTestId("header-role-handle")).toHaveTextContent("forthkit/Product");
  });

  it("drops the handle when the loaded list says this session does not hold it", async () => {
    api.listNamespaceRoles.mockResolvedValue([
      role({ name: "Scratch", state: "vacant", path: "", handle: "/Scratch" }),
    ]);
    renderWithQuery(<HeaderRoleHandle conversationId="s" namespacePath="" alias="Scratch" />);
    await waitFor(() => expect(screen.queryByTestId("header-role-handle")).toBeNull());
  });
});

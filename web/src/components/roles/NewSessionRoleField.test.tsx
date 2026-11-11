import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { NamespaceRole } from "@/lib/rolesApi";
import { NewSessionRoleField, heldRoleNamed } from "./NewSessionRoleField";

const release = vi.fn();
const roles: NamespaceRole[] = [
  {
    roleId: "role_1",
    name: "Architect",
    handle: "Harbor/Architect",
    path: "Harbor",
    permanent: true,
    state: "held",
    holderSessionId: "conv_holder",
  },
  {
    roleId: "role_2",
    name: "Bench",
    handle: "Harbor/Bench",
    path: "Harbor",
    permanent: true,
    state: "vacant",
    holderSessionId: null,
  },
];
vi.mock("@/hooks/useRoles", () => ({
  useNamespaceRoles: () => ({ data: roles }),
  useReleaseRole: () => ({ mutate: release, isPending: false, error: null }),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { title: "Design review" } }),
}));

describe("heldRoleNamed", () => {
  it("matches a held role in any case and ignores vacant roles and blanks", () => {
    expect(heldRoleNamed(roles, " architect ")?.roleId).toBe("role_1");
    expect(heldRoleNamed(roles, "Bench")).toBeNull();
    expect(heldRoleNamed(roles, "  ")).toBeNull();
  });
});

describe("NewSessionRoleField", () => {
  it("suggests the namespace's roles and stays quiet for a vacant one", () => {
    render(<NewSessionRoleField namespacePath="Harbor" value="Bench" onChange={() => {}} />);
    const options = Array.from(document.querySelectorAll("datalist option")).map(
      (option) => (option as HTMLOptionElement).value,
    );
    expect(options).toEqual(["Architect", "Bench"]);
    expect(screen.queryByTestId("new-session-role-held")).toBeNull();
  });

  it("names the holder of a held role and releases it on request", () => {
    render(<NewSessionRoleField namespacePath="Harbor" value="Architect" onChange={() => {}} />);
    expect(screen.getByTestId("new-session-role-held").textContent).toContain(
      "Harbor/Architect is held by Design review",
    );
    fireEvent.click(screen.getByTestId("new-session-role-release"));
    expect(release).toHaveBeenCalledWith("conv_holder");
  });
});

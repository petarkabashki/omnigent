import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DropdownMenu, DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { ProjectPicker } from "./ProjectPicker";

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [
      { id: "p_a", name: "a", parent_id: null, path: "a" },
      { id: "p_sub", name: "sub", parent_id: "p_a", path: "a/sub" },
      { id: "p_b", name: "b", parent_id: null, path: "b" },
      { id: "p_b_sub", name: "sub", parent_id: "p_b", path: "b/sub" },
    ],
  }),
}));

afterEach(cleanup);

function renderPicker(onSelect: (project: string) => void, currentProject: string | null = null) {
  render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <ProjectPicker currentProject={currentProject} onSelect={onSelect} />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
}

describe("ProjectPicker paths", () => {
  it("lists nested projects by path and selects the path", () => {
    const onSelect = vi.fn();
    renderPicker(onSelect);

    fireEvent.click(screen.getByRole("menuitem", { name: "b/sub" }));

    expect(onSelect).toHaveBeenCalledWith("b/sub");
  });

  it("finds a nested project by a parent segment", () => {
    renderPicker(vi.fn());

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a/" } });

    expect(screen.getByRole("menuitem", { name: "a/sub" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "b/sub" })).toBeNull();
  });

  it("offers create for a new single name but never for a path", () => {
    renderPicker(vi.fn());
    const input = screen.getByRole("textbox");

    fireEvent.change(input, { target: { value: "fresh" } });
    expect(screen.getByRole("menuitem", { name: /fresh/ })).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "a/fresh" } });
    expect(screen.queryByRole("menuitem", { name: /fresh/ })).toBeNull();
  });
});

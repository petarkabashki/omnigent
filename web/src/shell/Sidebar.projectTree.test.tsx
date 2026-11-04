import type * as DndKitCore from "@dnd-kit/core";
import type * as ToastModule from "@/components/ui/toast";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { act, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

const mocks = vi.hoisted(() => ({
  dragEnd: undefined as undefined | ((event: unknown) => void),
  saveOrder: vi.fn(),
  toast: vi.fn(),
  folderSessions: vi.fn(),
}));

// jsdom has no layout, so drops go straight to the sidebar's drag-end handler.
vi.mock("@dnd-kit/core", async (importOriginal) => {
  const actual = await importOriginal<typeof DndKitCore>();
  const { createElement } = await import("react");
  return {
    ...actual,
    DndContext: (props: Parameters<typeof actual.DndContext>[0]) => {
      mocks.dragEnd = props.onDragEnd as (event: unknown) => void;
      return createElement(actual.DndContext, props);
    },
  };
});
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof ToastModule>()),
  showToast: mocks.toast,
}));
vi.mock("@/hooks/useProjectOrder", () => ({
  useProjectOrder: () => ({ data: { sort_mode: "manual", ordered_project_ids: null } }),
  useSaveProjectOrder: () => ({ mutate: mocks.saveOrder, isPending: false }),
}));
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  const hooks = conversationHooksMock();
  return {
    ...hooks,
    useProjects: () => ({
      data: [
        { id: "p_a", name: "a", parent_id: null, path: "a" },
        { id: "p_one", name: "one", parent_id: "p_a", path: "a/one" },
        { id: "p_two", name: "two", parent_id: "p_a", path: "a/two" },
        { id: "p_b", name: "b", parent_id: null, path: "b" },
      ],
    }),
    useProjectSessions: (project: string, enabled: boolean) => {
      mocks.folderSessions(project, enabled);
      return hooks.useProjectSessions();
    },
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { EXPANDED_PROJECT_SECTIONS_STORAGE_KEY } from "./sidebarNav";

const drop = (active: string, over: string) =>
  act(() =>
    mocks.dragEnd?.({
      active: { data: { current: { type: "project-order", name: active } } },
      over: { data: { current: { type: "project-order", name: over } } },
    }),
  );

beforeEach(() => {
  vi.mocked(useConversations).mockImplementation(() => conversationPage([]));
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("dragging projects across tree levels", () => {
  it("keeps a child in place and points to Move under… when dropped on a root", () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["a"]));
    renderSidebar();

    drop("a/one", "b");

    expect(mocks.saveOrder).not.toHaveBeenCalled();
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast.mock.calls[0][0]).toContain("Move under…");
  });

  it("still reorders siblings silently", () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["a"]));
    renderSidebar();

    drop("a/one", "a/two");

    expect(mocks.saveOrder).toHaveBeenCalledTimes(1);
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});

describe("nested folder fetching", () => {
  const enabledFor = (project: string) =>
    mocks.folderSessions.mock.calls
      .filter(([name]) => name === project)
      .map(([, enabled]) => enabled);

  it("does not fetch a folder marked expanded while its parent is collapsed", () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["a/one"]));
    renderSidebar();

    expect(enabledFor("one").length).toBeGreaterThan(0);
    expect(enabledFor("one")).not.toContain(true);
  });

  it("fetches it once the parent is expanded too", () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["a", "a/one"]));
    renderSidebar();

    expect(enabledFor("one")).toContain(true);
  });
});

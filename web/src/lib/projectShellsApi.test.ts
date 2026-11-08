import { beforeEach, describe, expect, it, vi } from "vitest";
import { parseProjectShellKey, projectShellTabKey } from "@/hooks/useProjectShells";

const authenticatedFetch = vi.fn();
vi.mock("./identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));

import {
  createProjectShell,
  deleteProjectShell,
  listProjectShells,
  projectShellAttachPath,
} from "./projectShellsApi";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("projectShellsApi", () => {
  beforeEach(() => authenticatedFetch.mockReset());

  it("lists a project's shells", async () => {
    authenticatedFetch.mockResolvedValue(json({ data: [{ id: "pshell_1" }] }));
    expect(await listProjectShells("p 1")).toEqual([{ id: "pshell_1" }]);
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/projects/p%201/shells");
  });

  it("creates with a JSON content type so a cross-site simple request is not possible", async () => {
    authenticatedFetch.mockResolvedValue(json({ id: "pshell_2" }, 201));
    await createProjectShell("p1");
    const [url, init] = authenticatedFetch.mock.calls[0];
    expect(url).toBe("/v1/projects/p1/shells");
    expect(init).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  it("deletes one shell", async () => {
    authenticatedFetch.mockResolvedValue(json({}));
    await deleteProjectShell("p1", "pshell_3");
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/projects/p1/shells/pshell_3", {
      method: "DELETE",
    });
  });

  it("raises on a failed request", async () => {
    authenticatedFetch.mockResolvedValue(json({ error: { message: "offline" } }, 503));
    await expect(createProjectShell("p1")).rejects.toBeTruthy();
  });

  it("builds the attach path and round-trips tab keys", () => {
    expect(projectShellAttachPath("p1", "pshell_4")).toBe("/v1/projects/p1/shells/pshell_4/attach");
    expect(parseProjectShellKey(projectShellTabKey("pshell_4"))).toBe("pshell_4");
    expect(parseProjectShellKey("terminal:bash_s1")).toBeNull();
  });
});

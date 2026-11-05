import { afterEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { listNamespaceRoles, releaseSessionRole, roleHandle, takeSessionRole } from "./rolesApi";

const fetchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/identity", async (importOriginal) => {
  const actual = await importOriginal<typeof IdentityModule>();
  return { ...actual, authenticatedFetch: fetchMock };
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  fetchMock.mockReset();
});

describe("roleHandle", () => {
  it("keeps a leading slash for the unfiled root", () => {
    expect(roleHandle("", "Scratch")).toBe("/Scratch");
  });

  it("joins a namespace path and the role name", () => {
    expect(roleHandle("forthkit", "Product")).toBe("forthkit/Product");
  });
});

describe("listNamespaceRoles", () => {
  it("reads GET /v1/sessions/aliases/roles and lets state win over a stale holder id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        namespace: "lab",
        data: [
          {
            role_id: "role-held",
            name: "Held",
            handle: "lab/Held",
            path: "lab",
            permanent: true,
            state: "held",
            holder_session_id: "sess-held",
          },
          {
            role_id: null,
            name: "Legacy",
            handle: "lab/Legacy",
            path: "lab",
            permanent: false,
            state: "held",
            holder_session_id: "sess-legacy",
          },
          {
            role_id: "role-vacant",
            name: "Vacant",
            handle: "lab/Vacant",
            path: "lab",
            permanent: true,
            state: "vacant",
            holder_session_id: "sess-dead",
          },
        ],
      }),
    );

    const roles = await listNamespaceRoles("lab");

    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/aliases/roles?namespace=lab");
    expect(String(fetchMock.mock.calls)).not.toContain("/v1/roles");
    expect(roles).toEqual([
      {
        roleId: "role-held",
        name: "Held",
        handle: "lab/Held",
        path: "lab",
        permanent: true,
        state: "held",
        holderSessionId: "sess-held",
      },
      {
        roleId: null,
        name: "Legacy",
        handle: "lab/Legacy",
        path: "lab",
        permanent: false,
        state: "held",
        holderSessionId: "sess-legacy",
      },
      {
        roleId: "role-vacant",
        name: "Vacant",
        handle: "lab/Vacant",
        path: "lab",
        permanent: true,
        state: "vacant",
        holderSessionId: null,
      },
    ]);
  });

  it("asks for the unfiled root as /", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ namespace: "/", data: [] }));
    await listNamespaceRoles("");
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/aliases/roles?namespace=%2F");
  });
});

describe("takeSessionRole", () => {
  it("PUTs the alias name and nothing else", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ session_id: "sess-1", alias: "Architect" }));
    await takeSessionRole("sess/1", "Architect");
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/sess%2F1/alias", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias: "Architect" }),
    });
    expect(String(fetchMock.mock.calls)).not.toContain("takeover");
    expect(String(fetchMock.mock.calls)).not.toContain("closePrevious");
  });

  it("keeps the server's conflict code and message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "conflict",
            message: "Role 'Architect' in project 'lab' is held by session sess-other",
          },
        },
        409,
      ),
    );
    await expect(takeSessionRole("sess-1", "Architect")).rejects.toMatchObject({
      code: "conflict",
      message: "Role 'Architect' in project 'lab' is held by session sess-other",
      status: 409,
    });
  });
});

describe("releaseSessionRole", () => {
  it("DELETEs the alias and accepts an empty 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(releaseSessionRole("sess-1")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/sess-1/alias", { method: "DELETE" });
  });
});

// Client for the role routes the role store serves. A live holder is a 409
// from PUT; the body has no takeover or close-previous flag.

import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export const ALIAS_LABEL_KEY = "omnigent.alias";

export type RoleState = "held" | "vacant";

/** One role in a namespace, held or vacant. `roleId` is null for a label with no row yet. */
export interface NamespaceRole {
  roleId: string | null;
  name: string;
  /** `<path>/<Role>`, or `/<Role>` when the namespace is the unfiled root. */
  handle: string;
  /** Namespace path; `""` is the unfiled root. */
  path: string;
  permanent: boolean;
  state: RoleState;
  holderSessionId: string | null;
}

interface WireRole {
  role_id?: string | null;
  name?: string;
  handle?: string;
  path?: string;
  permanent?: boolean;
  state?: string;
  holder_session_id?: string | null;
}

/** Handle for a role. The unfiled root keeps the leading slash (`/Scratch`). */
export function roleHandle(path: string, name: string): string {
  return `${path}/${name}`;
}

function mapRole(wire: WireRole): NamespaceRole {
  const holderSessionId = wire.holder_session_id ?? null;
  // `state` is the server's liveness decision. A dead holder is vacant even
  // when an id is still on the row, so the chip must not read the id alone.
  const state: RoleState =
    wire.state === "held" || wire.state === "vacant"
      ? wire.state
      : holderSessionId
        ? "held"
        : "vacant";
  const path = wire.path ?? "";
  const name = wire.name ?? "";
  return {
    roleId: wire.role_id ?? null,
    name,
    handle: wire.handle || roleHandle(path, name),
    path,
    permanent: wire.permanent === true,
    state,
    holderSessionId: state === "vacant" ? null : holderSessionId,
  };
}

/** `GET /v1/sessions/aliases/roles`. `""` is the unfiled root, sent as `/`. */
export async function listNamespaceRoles(namespacePath: string): Promise<NamespaceRole[]> {
  const namespace = namespacePath === "" ? "/" : namespacePath;
  const params = new URLSearchParams({ namespace });
  const res = await authenticatedFetch(`/v1/sessions/aliases/roles?${params}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { data?: WireRole[] };
  return (body.data ?? []).map(mapRole);
}

/** `PUT /v1/sessions/{id}/alias`. Claims `alias` in the session's own namespace. */
export async function takeSessionRole(sessionId: string, alias: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/alias`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ alias }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/** `DELETE /v1/sessions/{id}/alias`. The route answers 204 with an empty body. */
export async function releaseSessionRole(sessionId: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/alias`, {
    method: "DELETE",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

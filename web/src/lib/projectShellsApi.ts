// Typed client for host-owned project shells (`/v1/projects/{id}/shells`,
// `omnigent/server/routes/project_shells.py`). Shells outlive sessions.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface ProjectShell {
  id: string;
  project_id: string;
  title: string;
  created_at: number;
  host_id: string;
  alive: boolean;
}

const base = (projectId: string) => `/v1/projects/${encodeURIComponent(projectId)}/shells`;

export async function listProjectShells(projectId: string): Promise<ProjectShell[]> {
  const res = await authenticatedFetch(base(projectId));
  if (!res.ok) throw await apiErrorFromResponse(res);
  return ((await res.json()) as { data: ProjectShell[] }).data;
}

export async function createProjectShell(projectId: string): Promise<ProjectShell> {
  const res = await authenticatedFetch(base(projectId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectShell;
}

export async function deleteProjectShell(projectId: string, shellId: string): Promise<void> {
  const res = await authenticatedFetch(`${base(projectId)}/${encodeURIComponent(shellId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/** Path + query of the websocket that attaches the browser to a project shell. */
export function projectShellAttachPath(projectId: string, shellId: string): string {
  return `${base(projectId)}/${encodeURIComponent(shellId)}/attach`;
}

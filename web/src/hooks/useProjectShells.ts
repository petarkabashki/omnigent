// Project shells for the workspace rail: host-owned shells that outlive
// sessions, behind the `project_shells` feature flag.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createProjectShell,
  deleteProjectShell,
  listProjectShells,
  type ProjectShell,
} from "@/lib/projectShellsApi";

const KEY_PREFIX = "pshell:";

/** Rail tab key for a project shell. */
export function projectShellTabKey(shellId: string): string {
  return `${KEY_PREFIX}${shellId}`;
}

/** The shell id behind a rail tab key, or null when the key is not a project shell. */
export function parseProjectShellKey(key: string): string | null {
  return key.startsWith(KEY_PREFIX) ? key.slice(KEY_PREFIX.length) : null;
}

const queryKey = (projectId: string | null) => ["project-shells", projectId];

export function useProjectShells(projectId: string | null, enabled: boolean) {
  return useQuery<ProjectShell[]>({
    queryKey: queryKey(projectId),
    queryFn: () => listProjectShells(projectId as string),
    enabled: enabled && projectId !== null,
    refetchInterval: 15_000,
    staleTime: 5_000,
    retry: false,
  });
}

export function useCreateProjectShell(projectId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => createProjectShell(projectId as string),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKey(projectId) }),
  });
}

export function useDeleteProjectShell(projectId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (shellId: string) => deleteProjectShell(projectId as string, shellId),
    onSuccess: (_void, shellId) =>
      client.setQueryData<ProjectShell[]>(queryKey(projectId), (old) =>
        (old ?? []).filter((s) => s.id !== shellId),
      ),
  });
}

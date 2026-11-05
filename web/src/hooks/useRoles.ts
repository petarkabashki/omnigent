import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  listNamespaceRoles,
  releaseSessionRole,
  takeSessionRole,
  type NamespaceRole,
} from "@/lib/rolesApi";

export const ROLES_QUERY_KEY = ["roles"] as const;

interface RoleCacheSnapshot {
  previous: [readonly unknown[], NamespaceRole[] | undefined][];
}

export function useNamespaceRoles(namespacePath: string) {
  return useQuery({
    queryKey: [...ROLES_QUERY_KEY, namespacePath],
    queryFn: () => listNamespaceRoles(namespacePath),
  });
}

function patchRoleLists(queryClient: QueryClient, mapRole: (role: NamespaceRole) => NamespaceRole) {
  for (const [key, data] of queryClient.getQueriesData<NamespaceRole[]>({
    queryKey: ROLES_QUERY_KEY,
  })) {
    if (!data) continue;
    queryClient.setQueryData(
      key,
      data.map((role) => mapRole(role)),
    );
  }
}

export function useTakeRole() {
  const queryClient = useQueryClient();
  return useMutation<
    void,
    Error,
    { sessionId: string; alias: string; namespacePath: string },
    RoleCacheSnapshot
  >({
    mutationFn: ({ sessionId, alias }) => takeSessionRole(sessionId, alias),
    onMutate: async ({ sessionId, alias, namespacePath }) => {
      await queryClient.cancelQueries({ queryKey: ROLES_QUERY_KEY });
      const previous = queryClient.getQueriesData<NamespaceRole[]>({ queryKey: ROLES_QUERY_KEY });
      patchRoleLists(queryClient, (role) =>
        role.name === alias && role.path === namespacePath
          ? { ...role, state: "held", holderSessionId: sessionId }
          : role,
      );
      return { previous };
    },
    onError: (_error, _input, context) => {
      for (const [key, data] of context?.previous ?? []) queryClient.setQueryData(key, data);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useReleaseRole() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string, RoleCacheSnapshot>({
    mutationFn: (sessionId) => releaseSessionRole(sessionId),
    onMutate: async (sessionId) => {
      await queryClient.cancelQueries({ queryKey: ROLES_QUERY_KEY });
      const previous = queryClient.getQueriesData<NamespaceRole[]>({ queryKey: ROLES_QUERY_KEY });
      patchRoleLists(queryClient, (role) =>
        role.holderSessionId === sessionId
          ? { ...role, state: "vacant", holderSessionId: null }
          : role,
      );
      return { previous };
    },
    onError: (_error, _sessionId, context) => {
      for (const [key, data] of context?.previous ?? []) queryClient.setQueryData(key, data);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}

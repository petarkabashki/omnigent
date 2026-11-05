import { toast } from "sonner";
import { useNamespaceRoles, useTakeRole } from "@/hooks/useRoles";
import type { NamespaceRole } from "@/lib/rolesApi";
import { RoleRow } from "./RoleRow";

/** Role rows for one namespace. Take claims a vacant name; a held name has no action. */
export function RolesList({
  namespacePath,
  sessionId,
}: {
  /** Project path, or `""` for the unfiled root. */
  namespacePath: string;
  sessionId?: string | null;
}) {
  const { data: roles = [], isError, error, isPending } = useNamespaceRoles(namespacePath);
  const take = useTakeRole();

  const handleTake = (role: NamespaceRole) => {
    if (!sessionId || role.state !== "vacant") return;
    take.mutate(
      { sessionId, alias: role.name, namespacePath },
      {
        onError: (takeError) => {
          toast.error(takeError.message);
        },
      },
    );
  };

  if (isPending) {
    return <p className="text-ui text-muted-foreground">Loading roles…</p>;
  }
  if (isError) {
    return <p className="text-ui text-muted-foreground">{error.message}</p>;
  }
  if (roles.length === 0) {
    return <p className="text-ui text-muted-foreground">No roles in this namespace.</p>;
  }

  const heading = namespacePath || "/ (unfiled)";
  return (
    <div data-testid="roles-list" className="flex flex-col gap-2">
      <section aria-label={namespacePath || "Unfiled"}>
        <h4 className="text-xs font-medium text-muted-foreground">{heading}</h4>
        {roles.map((role) => (
          <RoleRow
            key={role.roleId ?? role.handle}
            role={role}
            sessionId={sessionId}
            disabled={take.isPending}
            onTake={handleTake}
          />
        ))}
      </section>
    </div>
  );
}

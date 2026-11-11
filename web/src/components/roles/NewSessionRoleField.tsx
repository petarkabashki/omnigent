import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { useSession } from "@/hooks/useSession";
import { useNamespaceRoles, useReleaseRole } from "@/hooks/useRoles";
import type { NamespaceRole } from "@/lib/rolesApi";

/** The role named `name` (any case) when a live session holds it, else null. */
export function heldRoleNamed(roles: NamespaceRole[], name: string): NamespaceRole | null {
  const wanted = name.trim().toLowerCase();
  if (!wanted) return null;
  return (
    roles.find(
      (role) => role.state === "held" && role.holderSessionId && role.name.toLowerCase() === wanted,
    ) ?? null
  );
}

interface NewSessionRoleFieldProps {
  /** Namespace the new session is filed under; `""` is the unfiled root. */
  namespacePath: string;
  value: string;
  onChange: (value: string) => void;
}

/** Optional role for a new session, with the namespace's roles as suggestions. */
export function NewSessionRoleField({ namespacePath, value, onChange }: NewSessionRoleFieldProps) {
  const listId = useId();
  // Roles load once the field is used, so an untouched dialog makes no request.
  const [touched, setTouched] = useState(false);
  const { data: roles = [] } = useNamespaceRoles(namespacePath, touched || value !== "");
  const held = heldRoleNamed(roles, value);
  return (
    <div className="flex w-full max-w-md flex-col gap-1" data-testid="new-session-role-field">
      <label className="flex items-center gap-2 text-sm text-muted-foreground">
        <span className="shrink-0">Role</span>
        <input
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onFocus={() => setTouched(true)}
          list={listId}
          placeholder="None"
          maxLength={64}
          autoComplete="off"
          data-testid="new-session-role-input"
          className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-ui text-foreground placeholder:text-muted-foreground/60"
        />
      </label>
      <datalist id={listId}>
        {roles.map((role) => (
          <option key={role.handle} value={role.name}>
            {role.state === "held" ? "held" : "vacant"}
          </option>
        ))}
      </datalist>
      {held?.holderSessionId ? <RoleHolderNotice role={held} /> : null}
    </div>
  );
}

function RoleHolderNotice({ role }: { role: NamespaceRole }) {
  const holderId = role.holderSessionId ?? "";
  const { session } = useSession(holderId);
  const release = useReleaseRole();
  const holderName = session?.title?.trim() || "another session";
  return (
    <p
      className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
      data-testid="new-session-role-held"
    >
      <span>
        {role.handle} is held by <strong className="text-foreground">{holderName}</strong>. Release
        it to start this session with the role.
      </span>
      {release.error ? <span className="text-destructive">{release.error.message}</span> : null}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={release.isPending}
        onClick={() => release.mutate(holderId)}
        data-testid="new-session-role-release"
      >
        Release
      </Button>
    </p>
  );
}

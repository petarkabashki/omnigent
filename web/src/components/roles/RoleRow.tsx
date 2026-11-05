import { Button } from "@/components/ui/button";
import type { NamespaceRole } from "@/lib/rolesApi";
import { RoleChip } from "./RoleChip";

/**
 * One role in a namespace. A vacant role can be taken. A live holder is left
 * alone: the server answers 409, and the web UI has no takeover call.
 */
export function RoleRow({
  role,
  sessionId,
  disabled,
  onTake,
}: {
  role: NamespaceRole;
  /** The session that would take a vacant role; omit to hide the action. */
  sessionId?: string | null;
  disabled?: boolean;
  onTake: (role: NamespaceRole) => void;
}) {
  const heldByThisSession = sessionId != null && role.holderSessionId === sessionId;
  const vacant = role.state === "vacant";
  return (
    <div data-testid="role-row" className="flex items-center justify-between gap-2 py-1">
      <div className="flex min-w-0 items-center gap-2">
        <RoleChip role={role} />
        <span className="truncate text-xs text-muted-foreground">{role.handle}</span>
      </div>
      {sessionId && vacant && !heldByThisSession && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() => onTake(role)}
        >
          Take
        </Button>
      )}
    </div>
  );
}

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useNamespaceRoles } from "@/hooks/useRoles";
import { roleHandle } from "@/lib/rolesApi";
import { RolesList } from "./RolesList";

/**
 * The session's role handle (`forthkit/Product`, or `/Scratch` when unfiled).
 * Callers render this at every width, outside the breadcrumb: the desktop title
 * slot is hidden below 768px, and the native shells hide the breadcrumb itself.
 */
export function HeaderRoleHandle({
  conversationId,
  namespacePath,
  alias,
}: {
  conversationId: string;
  /** Project path, or `""` for the unfiled root. */
  namespacePath: string;
  /** Mirror label, shown until the role list confirms the holder. */
  alias: string;
}) {
  const { data, isSuccess } = useNamespaceRoles(namespacePath);
  const held = data?.find((role) => role.holderSessionId === conversationId);
  // A list that has loaded is the authority. A stale alias label must not keep
  // the handle up after the row says this session does not hold it.
  if (isSuccess && !held) return null;
  const handle = held?.handle ?? roleHandle(namespacePath, alias);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="header-role-handle"
          aria-label={`Role: ${handle}`}
          className="shrink-0 cursor-pointer rounded-full border border-primary px-2 py-0.5 text-xs text-primary hover:bg-primary/10 focus-visible:outline-none"
        >
          {handle}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80">
        <RolesList namespacePath={namespacePath} sessionId={conversationId} />
      </PopoverContent>
    </Popover>
  );
}

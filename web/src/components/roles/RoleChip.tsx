import { cn } from "@/lib/utils";
import type { NamespaceRole } from "@/lib/rolesApi";

/** Filled when the role is held, hollow when vacant. `state` decides, not the holder id. */
export function RoleChip({ role, className }: { role: NamespaceRole; className?: string }) {
  const held = role.state === "held";
  return (
    <span
      data-testid="role-chip"
      data-state={held ? "held" : "vacant"}
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        held
          ? "border-primary bg-primary text-primary-foreground"
          : "border-muted-foreground/60 bg-transparent text-muted-foreground",
        className,
      )}
    >
      {role.name}
      <span className="sr-only">{held ? " (held)" : " (vacant)"}</span>
    </span>
  );
}

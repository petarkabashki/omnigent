import { FolderIcon, GitForkIcon } from "lucide-react";
import { PresenceAvatars } from "@/components/PresenceAvatars";
import { DropdownMenuLabel, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { ForkOfSession } from "./ForkOfSession";
import { basename } from "./WorkspacePicker";

export function SessionMenuHeader({
  title,
  workingDirectory,
  forkOfSessionId,
}: {
  title: string;
  workingDirectory?: string | null;
  /** A fork names its parent here instead of a workspace it does not have. */
  forkOfSessionId?: string | null;
}) {
  return (
    <>
      <DropdownMenuLabel className="flex items-center gap-2 px-2.5 pb-1.5 text-foreground">
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <PresenceAvatars />
      </DropdownMenuLabel>
      {workingDirectory && (
        <DropdownMenuLabel
          className="flex items-center gap-2.5 px-2.5 py-2 text-ui font-normal text-popover-foreground"
          title={workingDirectory}
          aria-label={`Working directory: ${workingDirectory}`}
        >
          <FolderIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="min-w-0 truncate">{basename(workingDirectory)}</span>
        </DropdownMenuLabel>
      )}
      {forkOfSessionId && !workingDirectory && (
        <DropdownMenuLabel className="flex items-center gap-2.5 px-2.5 py-2 text-ui font-normal text-popover-foreground">
          <GitForkIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <ForkOfSession sessionId={forkOfSessionId} className="text-ui" />
        </DropdownMenuLabel>
      )}
      <DropdownMenuSeparator />
    </>
  );
}

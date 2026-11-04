import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCreateProject, useMoveProject } from "@/hooks/useConversations";
import { pathSegments } from "@/lib/projectTree";

/** A candidate parent in the move dialog: its id and full namespace path. */
export interface ParentOption {
  id: string;
  path: string;
}

/** The project's namespace path as breadcrumb segments, the last one current. */
export function ProjectBreadcrumb({ path }: { path: string }) {
  const segments = pathSegments(path);
  return (
    <span
      role="navigation"
      aria-label="Project path"
      className="inline-flex min-w-0 items-center gap-0.5 align-middle"
    >
      {segments.map((segment, index) => (
        <span
          key={segments.slice(0, index + 1).join("/")}
          className="inline-flex min-w-0 items-center gap-0.5"
        >
          {index > 0 && <ChevronRightIcon className="size-3 shrink-0 text-muted-foreground" />}
          <span
            className={index === segments.length - 1 ? "truncate font-medium" : "truncate"}
            aria-current={index === segments.length - 1 ? "page" : undefined}
          >
            {segment}
          </span>
        </span>
      ))}
    </span>
  );
}

/** Create a project nested under `parent`. */
export function CreateChildProjectDialog({
  open,
  onOpenChange,
  parent,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  parent: { id: string; path: string };
  onCreated?: (path: string) => void;
}) {
  const [name, setName] = useState("");
  const create = useCreateProject();
  const trimmed = name.trim();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>New child project</DialogTitle>
          <DialogDescription>
            Created inside <ProjectBreadcrumb path={parent.path} />
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!trimmed) return;
            create.mutate(
              { name: trimmed, parentId: parent.id },
              {
                onSuccess: (created) => {
                  setName("");
                  onOpenChange(false);
                  onCreated?.(created.path ?? `${parent.path}/${trimmed}`);
                },
              },
            );
          }}
        >
          <input
            aria-label="Child project name"
            data-testid="create-child-project-name"
            className="w-full rounded-lg border border-input bg-transparent px-3 py-2 text-ui outline-none"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
          {create.isError && (
            <p className="mt-2 text-ui text-destructive" role="alert">
              {(create.error as Error).message}
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              data-testid="create-child-project-confirm"
              loading={create.isPending}
              disabled={!trimmed}
              componentId="sidebar.project.create_child"
            >
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Pick a new parent (or the top level) for a project; the caller excludes its own subtree. */
export function MoveProjectDialog({
  open,
  onOpenChange,
  project,
  currentParentId,
  options,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  project: { id: string; path: string };
  currentParentId: string | null;
  options: ParentOption[];
}) {
  const [target, setTarget] = useState<string | null>(currentParentId);
  const move = useMoveProject();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Move project</DialogTitle>
          <DialogDescription>
            Choose where <ProjectBreadcrumb path={project.path} /> should live.
          </DialogDescription>
        </DialogHeader>
        <select
          aria-label="New parent project"
          data-testid="move-project-parent"
          className="w-full rounded-lg border border-input bg-transparent px-3 py-2 text-ui"
          value={target ?? ""}
          onChange={(e) => setTarget(e.target.value || null)}
        >
          <option value="">Top level</option>
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {option.path}
            </option>
          ))}
        </select>
        {move.isError && (
          <p className="text-ui text-destructive" role="alert">
            {(move.error as Error).message}
          </p>
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            data-testid="move-project-confirm"
            loading={move.isPending}
            disabled={target === currentParentId}
            componentId="sidebar.project.move_parent"
            onClick={() =>
              move.mutate(
                { id: project.id, parentId: target },
                { onSuccess: () => onOpenChange(false) },
              )
            }
          >
            Move
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

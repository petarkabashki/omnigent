import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useNamespaceRoles } from "@/hooks/useRoles";
import { useNavigate } from "@/lib/routing";
import { createSideChat } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";

interface OpenThreadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The role-holding session the thread is opened from. */
  holderId: string;
  /** Namespace path of the holder's role; `""` is the unfiled root. */
  namespacePath: string;
}

/** Pick (or type) the counterpart role, then open a thread with it in the side chat. */
export function OpenThreadDialog({
  open,
  onOpenChange,
  holderId,
  namespacePath,
}: OpenThreadDialogProps) {
  const navigate = useNavigate();
  const [handle, setHandle] = useState("");
  const [pending, setPending] = useState(false);
  const { data: roles = [] } = useNamespaceRoles(namespacePath, open);
  const choices = roles.filter((role) => role.holderSessionId !== holderId);

  const openThread = async (rawHandle: string) => {
    const thread = rawHandle.trim();
    if (!thread || pending) return;
    setPending(true);
    try {
      navigate(`/c/${holderId}`);
      const { childSessionId } = await createSideChat(holderId, undefined, { thread });
      useChatStore.getState().openSideChatWithDraft(childSessionId, "", holderId);
      onOpenChange(false);
      setHandle("");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't open the thread.");
    } finally {
      setPending(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void openThread(handle);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>Open thread</DialogTitle>
            <DialogDescription>
              Pick the counterpart role, or enter its handle, e.g. Architect.
            </DialogDescription>
          </DialogHeader>
          {choices.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2" data-testid="open-thread-roles">
              {choices.map((role) => (
                <Button
                  key={role.handle}
                  type="button"
                  variant="outline"
                  size="sm"
                  data-testid="open-thread-role"
                  disabled={pending}
                  onClick={() => void openThread(role.handle)}
                >
                  {role.name}
                </Button>
              ))}
            </div>
          )}
          <input
            autoFocus
            aria-label="Counterpart role handle"
            data-testid="header-open-thread-input"
            value={handle}
            onChange={(event) => setHandle(event.target.value)}
            className="mt-4 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50"
          />
          <DialogFooter className="mt-4 border-t-0 bg-transparent mobile-app:flex-col-reverse mobile-app:gap-1">
            <Button
              type="button"
              variant="ghost"
              className="mobile-touch-target mobile-app:w-full"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              className="mobile-touch-target mobile-app:w-full"
              disabled={!handle.trim() || pending}
            >
              Open
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

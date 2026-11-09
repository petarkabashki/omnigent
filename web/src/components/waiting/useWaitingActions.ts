import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { nudgeRoleThread } from "@/lib/roleThreads";
import { useNavigate } from "@/lib/routing";
import { interrupt } from "@/lib/sessionsApi";
import type { WaitingItem } from "@/lib/waitingApi";
import { threadFromWaitingItem } from "./WaitingItemRow";

/** Open / Nudge / Stop handlers shared by the header popover and the Waiting page. */
export function useWaitingActions() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["waiting"] });
  return {
    onOpen: (sessionId: string) => navigate(`/c/${encodeURIComponent(sessionId)}`),
    onNudge: (item: WaitingItem) => {
      nudgeRoleThread(item.session_id, threadFromWaitingItem(item)).then(
        () => {
          toast.info("Nudge sent");
          void refresh();
        },
        (err: unknown) => toast.error(err instanceof Error ? err.message : "Nudge failed"),
      );
    },
    onStop: (sessionId: string) => {
      interrupt(sessionId).then(
        () => {
          toast.info("Stop requested");
          void refresh();
        },
        (err: unknown) => toast.error(err instanceof Error ? err.message : "Stop failed"),
      );
    },
  };
}

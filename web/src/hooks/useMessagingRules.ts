import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** The messaging_rules file as served by ``GET /v1/messaging-rules``. */
export interface MessagingRulesFile {
  policy: string;
  path: string;
  text: string;
  valid: boolean;
  error: string | null;
}

export interface MessagingPairResult {
  action: "ALLOW" | "ASK" | "DENY";
  rule_index: number | null;
  reason: string;
}

const QUERY_KEY = ["messaging-rules"];

async function failure(res: Response): Promise<Error> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: string };
    detail = body.error?.message ?? body.detail ?? detail;
  } catch {
    // Non-JSON error body: keep the status line.
  }
  return new Error(detail);
}

/** Fetch the active messaging rules file; rejects with a 404 message when none is enabled. */
export function useMessagingRules() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: async (): Promise<MessagingRulesFile> => {
      const res = await authenticatedFetch("/v1/messaging-rules");
      if (!res.ok) throw await failure(res);
      return (await res.json()) as MessagingRulesFile;
    },
    retry: false,
  });
}

/** PUT /v1/messaging-rules — the server rejects invalid text with its parse error. */
export function useSaveMessagingRules() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (text: string) => {
      const res = await authenticatedFetch("/v1/messaging-rules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw await failure(res);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

/** POST /v1/messaging-rules/test — decide a pair against the (optionally unsaved) text. */
export function useTestMessagingPair() {
  return useMutation({
    mutationFn: async (input: {
      from: string;
      to: string;
      text?: string;
    }): Promise<MessagingPairResult> => {
      const res = await authenticatedFetch("/v1/messaging-rules/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
      if (!res.ok) throw await failure(res);
      return (await res.json()) as MessagingPairResult;
    },
  });
}

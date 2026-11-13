// Per-device memory of which role rows show their threads; collapsed by default.

import type { Conversation } from "@/hooks/useConversations";
import { ALIAS_LABEL_KEY } from "@/lib/rolesApi";
import { createStoredPreference } from "@/lib/storedChoicePreference";

const expandedRoles = createStoredPreference<string>(
  "omnigent:role-threads-expanded",
  (stored) => (parseKeys(stored).length > 0 ? stored : undefined),
  "",
);

function parseKeys(stored: string): string[] {
  try {
    const parsed: unknown = JSON.parse(stored);
    return Array.isArray(parsed) ? parsed.filter((key) => typeof key === "string") : [];
  } catch {
    return [];
  }
}

/** The role a row holds, so the choice follows the role to a new holder: `<project>/<alias>`. */
export function roleThreadsExpansionKey(conversation: Conversation): string {
  const alias = conversation.labels?.[ALIAS_LABEL_KEY]?.trim() || conversation.id;
  return `${conversation.project_id ?? ""}/${alias}`;
}

/** Whether a role's threads are shown under its row. */
export function useRoleThreadsExpanded(key: string): boolean {
  return parseKeys(expandedRoles.use()).includes(key);
}

/** Show or hide a role's threads, remembered across reloads. */
export function setRoleThreadsExpanded(key: string, expanded: boolean): void {
  const keys = parseKeys(expandedRoles.read()).filter((existing) => existing !== key);
  if (expanded) keys.push(key);
  expandedRoles.write(keys.length > 0 ? JSON.stringify(keys) : "");
}

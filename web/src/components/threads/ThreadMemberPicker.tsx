import { useId, useMemo, useState, type KeyboardEvent } from "react";
import { useQueries } from "@tanstack/react-query";
import { XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConversations, useProjects } from "@/hooks/useConversations";
import { ROLES_QUERY_KEY } from "@/hooks/useRoles";
import { memberFromText, memberLabel, type GroupThreadMemberKind } from "@/lib/groupThreads";
import { listNamespaceRoles } from "@/lib/rolesApi";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { cn } from "@/lib/utils";

/** A member to add: a role handle or a session id. */
export interface MemberChoice {
  kind: GroupThreadMemberKind;
  ref: string;
}

/** One searchable suggestion. */
export interface MemberOption extends MemberChoice {
  label: string;
  detail: string;
}

const MAX_SHOWN = 8;
const NO_MEMBERS: readonly MemberChoice[] = [];

/** Options matching `query` by label or detail, roles first; all of them for an empty query. */
export function filterMemberOptions(
  options: readonly MemberOption[],
  query: string,
  exclude: readonly MemberChoice[],
): MemberOption[] {
  const needle = query.trim().toLowerCase();
  return options.filter(
    (option) =>
      !exclude.some((chosen) => chosen.kind === option.kind && chosen.ref === option.ref) &&
      (!needle ||
        option.label.toLowerCase().includes(needle) ||
        option.detail.toLowerCase().includes(needle)),
  );
}

/** Every role in the unfiled root and each project, plus the caller's recent sessions. */
export function useMemberOptions(enabled = true): MemberOption[] {
  const { data: projects = [] } = useProjects({ enabled });
  const paths = useMemo(
    () => ["", ...new Set(projects.map((project) => project.path ?? "").filter(Boolean))],
    [projects],
  );
  const roleQueries = useQueries({
    queries: paths.map((path) => ({
      queryKey: [...ROLES_QUERY_KEY, path],
      queryFn: () => listNamespaceRoles(path),
      enabled,
      staleTime: 30_000,
    })),
  });
  const sessions = useConversations("", false, { enabled });
  const options: MemberOption[] = [];
  for (const query of roleQueries) {
    for (const role of query.data ?? []) {
      options.push({
        kind: "role",
        ref: role.handle,
        label: role.handle,
        detail: role.state === "held" ? "role" : "role · vacant",
      });
    }
  }
  for (const page of sessions.data?.pages ?? []) {
    for (const conversation of page.data) {
      options.push({
        kind: "session",
        ref: conversation.id,
        label: conversationDisplayLabel(conversation),
        detail: `session ${conversation.id.slice(0, 8)}`,
      });
    }
  }
  return options;
}

/**
 * A search box over roles and sessions. Picking a suggestion, or pressing Enter
 * on a typed `project/Role` handle or session id, calls `onPick`.
 */
export function MemberSearch({
  options,
  exclude = NO_MEMBERS,
  onPick,
  placeholder = "Search roles and sessions…",
  label = "Add member",
  className,
}: {
  options: readonly MemberOption[];
  exclude?: readonly MemberChoice[];
  onPick: (choice: MemberChoice) => void;
  placeholder?: string;
  label?: string;
  className?: string;
}) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const matches = filterMemberOptions(options, query, exclude).slice(0, MAX_SHOWN);

  const pick = ({ kind, ref }: MemberChoice) => {
    onPick({ kind, ref });
    setQuery("");
    setActive(0);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current) => (current + step + matches.length) % Math.max(matches.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const match = open ? matches[active] : undefined;
      if (match) pick(match);
      else if (query.trim()) pick(memberFromText(query));
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div className={cn("relative", className)}>
      <input
        role="combobox"
        aria-label={label}
        aria-expanded={open && matches.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        placeholder={placeholder}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
          setActive(0);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
        className="w-full rounded border border-border bg-background px-2 py-1.5 text-ui placeholder:text-muted-foreground/60"
      />
      {open && matches.length > 0 && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Suggestions"
          className="absolute z-20 mt-1 max-h-72 w-full overflow-y-auto rounded border border-border bg-popover py-1 shadow-md"
        >
          {matches.map((option, index) => (
            <li
              key={`${option.kind}:${option.ref}`}
              role="option"
              aria-selected={index === active}
              // Keep focus in the input so blur doesn't close the list before the click lands.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pick(option)}
              onMouseEnter={() => setActive(index)}
              className={cn(
                "flex cursor-pointer items-baseline gap-2 px-2 py-1 text-sm",
                index === active && "bg-muted",
              )}
            >
              <span className="min-w-0 truncate">{option.label}</span>
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                {option.detail}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Chosen members as removable chips, with a search box to add more. */
export function MemberPicker({
  value,
  onChange,
  options,
}: {
  value: MemberChoice[];
  onChange: (members: MemberChoice[]) => void;
  options: readonly MemberOption[];
}) {
  const labelFor = (choice: MemberChoice) =>
    options.find((option) => option.kind === choice.kind && option.ref === choice.ref)?.label ??
    memberLabel(choice);
  return (
    <div className="flex flex-col gap-1.5">
      {value.length > 0 && (
        <ul aria-label="Chosen members" className="flex flex-wrap gap-1.5">
          {value.map((choice) => (
            <li
              key={`${choice.kind}:${choice.ref}`}
              data-testid="chosen-member"
              className="flex max-w-full items-center gap-1 rounded-full border border-border py-0.5 pr-0.5 pl-2.5 text-sm"
            >
              <span className="min-w-0 truncate">{labelFor(choice)}</span>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={`Remove ${labelFor(choice)}`}
                onClick={() => onChange(value.filter((other) => other !== choice))}
              >
                <XIcon aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <MemberSearch
        label="Members"
        options={options}
        exclude={value}
        onPick={(choice) => onChange([...value, choice])}
      />
    </div>
  );
}

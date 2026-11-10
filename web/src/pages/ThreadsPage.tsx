/**
 * Group threads (``/threads`` and ``/threads/:threadKey``): the owner starts a
 * thread with roles and sessions, posts into it, and invites or removes members.
 */

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SendIcon, XIcon } from "lucide-react";
import { toast } from "sonner";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  POSTS_PAGE,
  createGroupThread,
  fetchGroupThread,
  fetchGroupThreadPosts,
  inviteToGroupThread,
  listGroupThreads,
  memberFromText,
  memberLabel,
  postAuthorLabel,
  postToGroupThread,
  removeFromGroupThread,
  updateGroupThread,
  type GroupThreadMember,
  type GroupThreadPrimer,
} from "@/lib/groupThreads";
import { relativeTime } from "@/lib/relativeTime";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { cn } from "@/lib/utils";

const FIELD_CLASS =
  "w-full rounded border border-border bg-background px-2 py-1.5 text-ui placeholder:text-muted-foreground/60";
// Members' posts arrive from agent turns the page cannot hear about; poll while open.
const REFRESH_MS = 5000;

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function ThreadsPage() {
  const { threadKey } = useParams<{ threadKey: string }>();
  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      {threadKey ? <ThreadView threadKey={threadKey} /> : <ThreadList />}
    </PageScroll>
  );
}

function ThreadList() {
  const threads = useQuery({ queryKey: ["group-threads"], queryFn: listGroupThreads });
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="mb-1 text-2xl font-medium">Threads</h1>
        <p className="text-ui text-muted-foreground">
          Conversations between you and several roles or sessions. Only the members a post names get
          a turn.
        </p>
      </div>
      <NewThreadForm />
      <section aria-label="Your threads" className="flex flex-col gap-1">
        {threads.isPending && <p className="text-ui text-muted-foreground">Loading…</p>}
        {threads.isError && (
          <p role="alert" className="text-ui text-destructive">
            {errorText(threads.error, "Couldn’t load threads.")}
          </p>
        )}
        {threads.data?.length === 0 && (
          <p className="text-ui text-muted-foreground">No threads yet.</p>
        )}
        {threads.data?.map((thread) => (
          <Link
            key={thread.key}
            to={`/threads/${thread.key}`}
            data-testid="group-thread-row"
            componentId="threads.open"
            className="flex flex-col gap-0.5 rounded border border-border px-3 py-2 hover:bg-muted"
          >
            <span className="flex items-center gap-2">
              <span className="min-w-0 truncate font-medium">{thread.title}</span>
              {thread.state === "done" && (
                <span className="text-xs text-muted-foreground">Done</span>
              )}
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                {thread.lastSeq} posts ·{" "}
                {relativeTime((thread.updatedAt ?? thread.createdAt) * 1000)}
              </span>
            </span>
            {thread.summary && (
              <span className="truncate text-sm text-muted-foreground">{thread.summary}</span>
            )}
          </Link>
        ))}
      </section>
    </div>
  );
}

function NewThreadForm() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [members, setMembers] = useState("");
  const [message, setMessage] = useState("");
  const create = useMutation({
    mutationFn: () =>
      createGroupThread({
        title: title.trim(),
        members: members
          .split(/[\n,]/)
          .filter((line) => line.trim())
          .map(memberFromText),
        message: message.trim(),
      }),
    onSuccess: async (thread) => {
      await queryClient.invalidateQueries({ queryKey: ["group-threads"] });
      navigate(`/threads/${thread.key}`);
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (title.trim() && members.trim()) create.mutate();
  };
  return (
    <form
      aria-label="New thread"
      onSubmit={submit}
      className="flex flex-col gap-2 rounded border border-border p-3"
    >
      <h2 className="text-lg font-medium">New thread</h2>
      <input
        aria-label="Title"
        placeholder="Title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        className={FIELD_CLASS}
      />
      <Textarea
        aria-label="Members"
        placeholder={"Members, one per line: omnigent/Product, /Architect or a session id"}
        value={members}
        onChange={(event) => setMembers(event.target.value)}
        className="min-h-16 text-sm"
      />
      <Textarea
        aria-label="First post"
        placeholder="First post (optional; goes to everyone)"
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        className="min-h-16 text-sm"
      />
      {create.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(create.error, "Couldn’t start the thread.")}
        </p>
      )}
      <div>
        <Button
          type="submit"
          loading={create.isPending}
          disabled={!title.trim() || !members.trim()}
          componentId="threads.create"
        >
          Start thread
        </Button>
      </div>
    </form>
  );
}

function ThreadView({ threadKey }: { threadKey: string }) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["group-thread", threadKey],
    queryFn: () => fetchGroupThread(threadKey),
    refetchInterval: REFRESH_MS,
  });
  const posts = useQuery({
    queryKey: ["group-thread-posts", threadKey],
    queryFn: () => fetchGroupThreadPosts(threadKey),
    refetchInterval: REFRESH_MS,
  });
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["group-thread", threadKey] }),
      queryClient.invalidateQueries({ queryKey: ["group-thread-posts", threadKey] }),
      queryClient.invalidateQueries({ queryKey: ["group-threads"] }),
    ]);
  const setState = useMutation({
    mutationFn: (state: "open" | "done") => updateGroupThread(threadKey, { state }),
    onSuccess: refresh,
    onError: (error) => toast.error(errorText(error, "Couldn’t update the thread.")),
  });

  if (detail.isPending) return <p className="text-ui text-muted-foreground">Loading…</p>;
  if (detail.isError) {
    return (
      <p role="alert" className="text-ui text-destructive">
        {errorText(detail.error, "Couldn’t load this thread.")}
      </p>
    );
  }
  const { thread, members } = detail.data;
  const firstSeq = posts.data?.[0]?.seq ?? 1;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <Link to="/threads" componentId="threads.back" className="text-sm text-muted-foreground">
            ← Threads
          </Link>
          <h1 className="text-2xl font-medium break-words">{thread.title}</h1>
          <code className="text-xs text-muted-foreground">{thread.key}</code>
        </div>
        <Button
          variant="outline"
          size="sm"
          loading={setState.isPending}
          onClick={() => setState.mutate(thread.state === "done" ? "open" : "done")}
          componentId="threads.state"
        >
          {thread.state === "done" ? "Reopen" : "Mark done"}
        </Button>
      </div>
      <SummaryEditor threadKey={threadKey} summary={thread.summary} onSaved={refresh} />
      <MemberList threadKey={threadKey} members={members} onChanged={refresh} />
      <section aria-label="Posts" className="flex flex-col gap-2">
        {posts.data && firstSeq > 1 && (
          <p className="text-xs text-muted-foreground">
            Showing the newest {POSTS_PAGE} posts; {firstSeq - 1} older ones are not shown.
          </p>
        )}
        {posts.data?.length === 0 && <p className="text-ui text-muted-foreground">No posts yet.</p>}
        {posts.isError && (
          <p role="alert" className="text-sm text-destructive">
            {errorText(posts.error, "Couldn’t load posts.")}
          </p>
        )}
        {posts.data?.map((post) => (
          <article
            key={post.id}
            data-testid="group-thread-post"
            data-author={post.authorKind}
            className={cn(
              "max-w-[90%] rounded-lg border border-border px-3 py-2 text-sm",
              post.authorKind === "owner"
                ? "self-end border-primary/40 bg-primary/5"
                : "self-start",
            )}
          >
            <p className="mb-1 text-xs text-muted-foreground">
              #{post.seq} {postAuthorLabel(post)} · {relativeTime(post.createdAt * 1000)}
              {post.mentions.length > 0 && <> → {post.mentions.join(", ")}</>}
            </p>
            <p className="break-words whitespace-pre-wrap">{post.text}</p>
          </article>
        ))}
      </section>
      <PostBox threadKey={threadKey} members={members} onPosted={refresh} />
    </div>
  );
}

function SummaryEditor({
  threadKey,
  summary,
  onSaved,
}: {
  threadKey: string;
  summary: string;
  onSaved: () => Promise<unknown>;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (text: string) => updateGroupThread(threadKey, { summary: text }),
    onSuccess: async () => {
      await onSaved();
      setDraft(null);
    },
    onError: (error) => toast.error(errorText(error, "Couldn’t save the summary.")),
  });
  if (draft === null) {
    return (
      <div className="flex items-start gap-2 text-sm">
        <p className="min-w-0 flex-1 text-muted-foreground">
          <span className="font-medium text-foreground">Summary: </span>
          {summary || "None yet. Late joiners get this as their primer."}
        </p>
        <Button
          variant="ghost"
          size="xs"
          onClick={() => setDraft(summary)}
          componentId="threads.summary.edit"
        >
          Edit
        </Button>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      <Textarea
        aria-label="Summary"
        value={draft}
        maxLength={2000}
        onChange={(event) => setDraft(event.target.value)}
        className="min-h-16 text-sm"
      />
      <div className="flex gap-2">
        <Button
          size="xs"
          loading={save.isPending}
          onClick={() => save.mutate(draft)}
          componentId="threads.summary.save"
        >
          Save
        </Button>
        <Button
          size="xs"
          variant="ghost"
          onClick={() => setDraft(null)}
          componentId="threads.summary.cancel"
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}

function MemberList({
  threadKey,
  members,
  onChanged,
}: {
  threadKey: string;
  members: GroupThreadMember[];
  onChanged: () => Promise<unknown>;
}) {
  const remove = useMutation({
    mutationFn: (memberId: string) => removeFromGroupThread(threadKey, memberId),
    onSuccess: onChanged,
    onError: (error) => toast.error(errorText(error, "Couldn’t remove the member.")),
  });
  return (
    <section aria-label="Members" className="flex flex-col gap-2">
      <ul className="flex flex-wrap gap-1.5">
        {members.map((member) => (
          <li
            key={member.id}
            data-testid="group-thread-member"
            data-state={member.state}
            title={
              member.state === "vacant"
                ? "Nobody holds this role; it catches up when someone takes it."
                : undefined
            }
            className={cn(
              "flex max-w-full items-center gap-1 rounded-full border border-border py-0.5 pr-0.5 pl-2.5 text-sm",
              member.state === "vacant" && "border-dashed text-muted-foreground",
            )}
          >
            {member.sessionId ? (
              <Link
                to={`/c/${member.sessionId}`}
                componentId="threads.member.open"
                className="min-w-0 truncate hover:underline"
              >
                {memberLabel(member)}
              </Link>
            ) : (
              <span className="min-w-0 truncate">{memberLabel(member)}</span>
            )}
            {member.state === "vacant" && <span className="text-xs">vacant</span>}
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Remove ${memberLabel(member)}`}
              disabled={remove.isPending}
              onClick={() => remove.mutate(member.id)}
              componentId="threads.member.remove"
            >
              <XIcon aria-hidden />
            </Button>
          </li>
        ))}
      </ul>
      <InviteForm threadKey={threadKey} onInvited={onChanged} />
    </section>
  );
}

const PRIMERS: [GroupThreadPrimer, string][] = [
  ["summary", "Summary"],
  ["full", "Full history"],
  ["none", "Nothing"],
];

function InviteForm({
  threadKey,
  onInvited,
}: {
  threadKey: string;
  onInvited: () => Promise<unknown>;
}) {
  const [ref, setRef] = useState("");
  const [primer, setPrimer] = useState<GroupThreadPrimer>("summary");
  const invite = useMutation({
    mutationFn: () => inviteToGroupThread(threadKey, memberFromText(ref), primer),
    onSuccess: async () => {
      setRef("");
      await onInvited();
    },
  });
  return (
    <form
      aria-label="Invite"
      className="flex flex-col gap-1"
      onSubmit={(event) => {
        event.preventDefault();
        if (ref.trim()) invite.mutate();
      }}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <input
          aria-label="Invite member"
          placeholder="Invite: project/Role or session id"
          value={ref}
          onChange={(event) => setRef(event.target.value)}
          className={cn(FIELD_CLASS, "min-w-0 flex-1 sm:max-w-72")}
        />
        <select
          aria-label="Primer"
          value={primer}
          onChange={(event) => setPrimer(event.target.value as GroupThreadPrimer)}
          className={cn(FIELD_CLASS, "w-auto")}
        >
          {PRIMERS.map(([value, label]) => (
            <option key={value} value={value}>
              Show: {label}
            </option>
          ))}
        </select>
        <Button
          type="submit"
          size="sm"
          variant="outline"
          loading={invite.isPending}
          disabled={!ref.trim()}
          componentId="threads.invite"
        >
          Invite
        </Button>
      </div>
      {invite.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(invite.error, "Couldn’t invite.")}
        </p>
      )}
    </form>
  );
}

function PostBox({
  threadKey,
  members,
  onPosted,
}: {
  threadKey: string;
  members: GroupThreadMember[];
  onPosted: () => Promise<unknown>;
}) {
  const [text, setText] = useState("");
  // Empty means everyone; otherwise the member ids chosen.
  const [chosen, setChosen] = useState<string[]>([]);
  const recipients = members.filter((member) => chosen.includes(member.id));
  const post = useMutation({
    mutationFn: () =>
      postToGroupThread(
        threadKey,
        text.trim(),
        recipients.length ? recipients.map((member) => member.ref) : "everyone",
      ),
    onSuccess: async () => {
      setText("");
      await onPosted();
    },
    onError: (error) => toast.error(errorText(error, "Couldn’t post.")),
  });
  const send = () => {
    if (text.trim() && !post.isPending) post.mutate();
  };
  const toggle = (memberId: string) =>
    setChosen((current) =>
      current.includes(memberId) ? current.filter((id) => id !== memberId) : [...current, memberId],
    );
  return (
    <form
      aria-label="Post to the thread"
      className="flex flex-col gap-1.5 border-t border-border pt-3"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <Textarea
        aria-label="Post"
        placeholder="Post to the thread…"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            send();
          }
        }}
        className="min-h-16 text-sm"
      />
      <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
        <span>To</span>
        <div role="group" aria-label="Post to" className="flex min-w-0 flex-wrap gap-0.5">
          <Button
            type="button"
            size="xs"
            aria-pressed={chosen.length === 0}
            variant={chosen.length === 0 ? "secondary" : "ghost"}
            onClick={() => setChosen([])}
            componentId="threads.post.to_everyone"
          >
            Everyone
          </Button>
          {members.map((member) => (
            <Button
              key={member.id}
              type="button"
              size="xs"
              aria-pressed={chosen.includes(member.id)}
              variant={chosen.includes(member.id) ? "secondary" : "ghost"}
              className="max-w-40 truncate"
              onClick={() => toggle(member.id)}
              componentId="threads.post.to_member"
            >
              {memberLabel(member)}
            </Button>
          ))}
        </div>
        <Button
          type="submit"
          size="xs"
          className="ml-auto"
          disabled={!text.trim() || post.isPending}
          title="Post (Ctrl+Enter)"
          componentId="threads.post.send"
        >
          {post.isPending ? (
            <Loader2Icon aria-hidden className="animate-spin" />
          ) : (
            <SendIcon aria-hidden />
          )}
          Post
        </Button>
      </div>
    </form>
  );
}

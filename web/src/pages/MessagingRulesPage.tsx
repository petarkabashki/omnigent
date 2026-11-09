/**
 * Admin editor for the messaging rules file (``/settings/messaging-rules``).
 *
 * Edits the YAML behind the Messaging Rules policy, with server-side
 * validation on save and a "Test a pair" form that decides a sender -> target
 * pair against the text in the editor. The server enforces admin access.
 */

import { useEffect, useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import {
  useMessagingRules,
  useSaveMessagingRules,
  useTestMessagingPair,
} from "@/hooks/useMessagingRules";

const FIELD_CLASS =
  "w-full rounded border border-border bg-background px-2 py-1.5 text-ui placeholder:text-muted-foreground/60";

export function MessagingRulesPage() {
  const { data, error, isLoading } = useMessagingRules();
  const save = useSaveMessagingRules();
  const test = useTestMessagingPair();
  const [text, setText] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    if (data) setText(data.text);
  }, [data]);

  const dirty = data !== undefined && text !== data.text;

  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      <h1 className="settings-page-title mb-1 text-2xl font-medium">Messaging rules</h1>
      <p className="mb-4 text-ui text-muted-foreground max-md:hidden">
        Who may message whom between sessions, by role handle. The first matching rule wins.
      </p>
      {isLoading && <p className="text-ui text-muted-foreground">Loading...</p>}
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error.message}
        </p>
      )}
      {data && (
        <div className="flex flex-col gap-6">
          <section className="flex flex-col gap-2">
            <code className="text-sm text-muted-foreground">{data.path}</code>
            {!data.valid && (
              <p role="alert" className="text-sm text-destructive">
                The saved file does not parse: {data.error}
              </p>
            )}
            <textarea
              aria-label="Rules file"
              spellCheck={false}
              value={text}
              onChange={(event) => setText(event.target.value)}
              className={`${FIELD_CLASS} h-96 font-mono text-sm`}
            />
            {save.isError && (
              <p role="alert" className="text-sm text-destructive">
                {save.error.message}
              </p>
            )}
            <div className="flex items-center gap-3">
              <Button
                onClick={() => save.mutate(text)}
                loading={save.isPending}
                disabled={!dirty}
                componentId="settings.messaging_rules.save"
              >
                Save
              </Button>
              {save.isSuccess && !dirty && <span className="text-sm">Saved</span>}
            </div>
          </section>
          <section className="flex flex-col gap-2">
            <h2 className="text-lg font-medium">Test a pair</h2>
            <div className="flex flex-wrap gap-2">
              <input
                aria-label="From"
                placeholder="from, e.g. forthkit/Product"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
                className={`${FIELD_CLASS} sm:w-64`}
              />
              <input
                aria-label="To"
                placeholder="to, e.g. touchkey/Coordinator"
                value={to}
                onChange={(event) => setTo(event.target.value)}
                className={`${FIELD_CLASS} sm:w-64`}
              />
              <Button
                variant="outline"
                onClick={() => test.mutate({ from, to, text })}
                loading={test.isPending}
                disabled={!from || !to}
                componentId="settings.messaging_rules.test"
              >
                Test
              </Button>
            </div>
            {test.isError && (
              <p role="alert" className="text-sm text-destructive">
                {test.error.message}
              </p>
            )}
            {test.data && (
              <p data-testid="pair-result" className="text-ui">
                <strong>{test.data.action}</strong> - {test.data.reason}
              </p>
            )}
          </section>
        </div>
      )}
    </PageScroll>
  );
}

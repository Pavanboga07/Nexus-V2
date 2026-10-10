import { useState, type FormEvent } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { checkLive, checkReady } from "../../lib/api/endpoints";
import { Button } from "../../components/ui/Button";
import { Field, TextInput } from "../../components/ui/Field";
import { Spinner } from "../../components/ui/Feedback";
import { PageHeader } from "../../components/ui/PageHeader";
import { AiProviderSection } from "./AiProviderSection";

/** Local API configuration and connection diagnostics. The token is
 *  never displayed back; it can only be replaced or cleared. */
export function SettingsPage() {
  const { baseUrl, setBaseUrl, saveToken, clearToken, testConnection, connection, token } =
    useAuth();
  const { streamState, streamError } = useEventBus();
  const [draftBase, setDraftBase] = useState(baseUrl);
  const [draftToken, setDraftToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [liveResult, setLiveResult] = useState<string | null>(null);
  const [readyResult, setReadyResult] = useState<string | null>(null);
  const [diagnosing, setDiagnosing] = useState(false);

  const saveBase = (event: FormEvent) => {
    event.preventDefault();
    setBaseUrl(draftBase.trim() || window.location.origin);
  };

  const replaceToken = async (event: FormEvent) => {
    event.preventDefault();
    if (draftToken.trim().length === 0) return;
    setSaving(true);
    saveToken(draftToken);
    setDraftToken("");
    await testConnection();
    setSaving(false);
  };

  const runDiagnostics = async () => {
    setDiagnosing(true);
    setLiveResult(null);
    setReadyResult(null);
    try {
      const live = await checkLive(baseUrl);
      setLiveResult(`Live endpoint: ${live.status}.`);
    } catch {
      setLiveResult("Live endpoint unreachable.");
    }
    try {
      const ready = await checkReady(baseUrl);
      setReadyResult(`Ready endpoint: ${ready.status}.`);
    } catch {
      setReadyResult("Ready endpoint not ready or unreachable.");
    }
    setDiagnosing(false);
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
      <PageHeader
        title="Settings"
        subtitle="API connection, credentials, and diagnostics for this workspace."
      />

      <section aria-labelledby="connection-heading" className="rounded-card border border-graphite-700 bg-graphite-900 p-5">
        <h2 id="connection-heading" className="text-base font-semibold text-ink-50">Connection</h2>
        <p className="mt-1 text-sm text-ink-500" role="status">
          {connection.kind === "connected" &&
            `Connected. ${connection.agentCount} local agents visible.`}
          {connection.kind === "checking" && "Checking connection."}
          {connection.kind === "failed" && `Connection failed: ${connection.message}`}
          {connection.kind === "unconfigured" && "No token saved."}
        </p>
        <form onSubmit={saveBase} className="mt-4 flex flex-col gap-3">
          <Field label="API base URL" hint="Public preference. Same-origin proxy by default.">
            <TextInput
              value={draftBase}
              onChange={(event) => setDraftBase(event.target.value)}
              inputMode="url"
              autoComplete="url"
              spellCheck={false}
            />
          </Field>
          <div>
            <Button variant="secondary" size="sm" type="submit">
              Save base URL
            </Button>
          </div>
        </form>
      </section>

      <section aria-labelledby="token-heading" className="mt-4 rounded-card border border-graphite-700 bg-graphite-900 p-5">
        <h2 id="token-heading" className="text-base font-semibold text-ink-50">Bearer token</h2>
        <p className="mt-1 text-sm text-ink-500">
          {token.length > 0
            ? "A token is saved in this tab. It is never displayed."
            : "No token saved in this tab."}
        </p>
        <form onSubmit={replaceToken} className="mt-4 flex flex-col gap-3">
          <Field label="Replace token" hint="Stored in session storage only.">
            <TextInput
              value={draftToken}
              onChange={(event) => setDraftToken(event.target.value)}
              type="password"
              autoComplete="current-password"
              spellCheck={false}
            />
          </Field>
          <div className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button variant="ghost" size="sm" onClick={clearToken} type="button">
              Clear token
            </Button>
            <Button variant="primary" size="sm" type="submit" busy={saving} disabled={draftToken.trim().length === 0}>
              {saving ? <Spinner label="Saving" /> : "Save and test"}
            </Button>
          </div>
        </form>
      </section>

      <AiProviderSection />

      <section aria-labelledby="events-heading" className="mt-4 rounded-card border border-graphite-700 bg-graphite-900 p-5">
        <h2 id="events-heading" className="text-base font-semibold text-ink-50">Live events</h2>
        <p className="mt-1 text-sm text-ink-500" role="status">
          Stream: {streamState}
          {streamError ? ` (${streamError})` : ""}
        </p>
        <p className="mt-2 text-xs leading-relaxed text-ink-600">
          Authenticated WebSocket on /api/v1/events/ws with a single-use
          ticket in the subprotocol header. Tickets are never placed in URLs.
          Events notify the UI to refresh from REST; they are not replayed
          after a disconnect, so the app resynchronizes automatically.
        </p>
      </section>

      <section aria-labelledby="diagnostics-heading" className="mt-4 rounded-card border border-graphite-700 bg-graphite-900 p-5">
        <h2 id="diagnostics-heading" className="text-base font-semibold text-ink-50">Diagnostics</h2>
        <div className="mt-3">
          <Button variant="secondary" size="sm" onClick={() => void runDiagnostics()} busy={diagnosing}>
            {diagnosing ? <Spinner label="Probing" /> : "Probe backend"}
          </Button>
        </div>
        {liveResult ? <p role="status" className="mt-2 text-sm text-ink-300">{liveResult}</p> : null}
        {readyResult ? <p role="status" className="mt-2 text-sm text-ink-300">{readyResult}</p> : null}
        <p className="mt-3 text-xs leading-relaxed text-ink-600">
          Single-owner deployment: one bearer token acts as the owner. Rotate
          it by replacing NEXUS_API_TOKEN on the backend and saving the new
          value here.
        </p>
      </section>
    </div>
  );
}

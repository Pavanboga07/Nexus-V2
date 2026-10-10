import { useState, type FormEvent } from "react";
import { useAuth } from "../lib/auth/AuthProvider";
import { ApiError } from "../lib/api/client";
import { checkLive } from "../lib/api/endpoints";
import { Button } from "../components/ui/Button";
import { Field, TextInput } from "../components/ui/Field";
import { ErrorState, Spinner } from "../components/ui/Feedback";

/** First-run connection experience: backend reachability, base URL
 *  (public preference), bearer token (password field, tab-scoped),
 *  and an explicit test-connection action. No mock login. */
export function AuthGate() {
  const { baseUrl, setBaseUrl, saveToken, testConnection, connection } = useAuth();
  const [draftBase, setDraftBase] = useState(baseUrl);
  const [draftToken, setDraftToken] = useState("");
  const [checking, setChecking] = useState(false);
  const [liveState, setLiveState] = useState<"unknown" | "up" | "down">("unknown");
  const [error, setError] = useState<string | null>(null);

  const probeLive = async () => {
    setLiveState("unknown");
    setError(null);
    try {
      await checkLive(draftBase.trim().replace(/\/+$/, "") || window.location.origin);
      setLiveState("up");
    } catch {
      setLiveState("down");
      setError("The backend did not answer. Start the API, then try again.");
    }
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (draftToken.trim().length === 0) {
      setError("Enter the API bearer token to continue.");
      return;
    }
    setError(null);
    setChecking(true);
    setBaseUrl(draftBase.trim() || window.location.origin);
    saveToken(draftToken);
    setDraftToken("");
    // NOTE: the result is intentionally ignored here. While checking, this
    // gate unmounts (the shell mounts optimistically), so local state set
    // after the await would be lost. The failure message below is derived
    // from the provider connection state, which survives the remount.
    await testConnection();
    setChecking(false);
  };

  const connectionError =
    connection.kind === "failed" ? connection.message : null;

  return (
    <div className="grid min-h-dvh place-items-center bg-graphite-950 px-4 py-10">
      <main className="w-full max-w-md rounded-card border border-graphite-700 bg-graphite-900 p-6 sm:p-8">
        <div className="mb-6 flex items-center gap-3">
          <span
            aria-hidden="true"
            className="grid h-10 w-10 place-items-center rounded-control bg-accent-500 text-lg font-bold text-white"
          >
            N
          </span>
          <div>
            <h1 className="text-xl font-semibold text-ink-50">Connect to Nexus</h1>
            <p className="text-sm text-ink-500">
              Point this workspace at your API deployment.
            </p>
          </div>
        </div>

        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field
            label="API base URL"
            hint="Same-origin proxy by default. Only change this for a remote deployment."
          >
            <TextInput
              value={draftBase}
              onChange={(event) => setDraftBase(event.target.value)}
              inputMode="url"
              autoComplete="url"
              spellCheck={false}
            />
          </Field>

          <div className="flex items-center gap-3 text-sm">
            <Button variant="ghost" size="sm" onClick={() => void probeLive()} type="button">
              Check backend
            </Button>
            {liveState === "up" ? (
              <span role="status" className="text-signal-green">Backend is reachable.</span>
            ) : null}
            {liveState === "down" ? (
              <span role="status" className="text-signal-red">Backend is unreachable.</span>
            ) : null}
          </div>

          <Field
            label="API bearer token"
            hint="Stored in this tab only. Never shown again after saving."
          >
            <TextInput
              value={draftToken}
              onChange={(event) => setDraftToken(event.target.value)}
              type="password"
              autoComplete="current-password"
              spellCheck={false}
            />
          </Field>

          {error ?? connectionError ? (
            <ErrorState
              title="Connection failed"
              body={(error ?? connectionError) as string}
            />
          ) : null}

          <Button variant="primary" type="submit" busy={checking}>
            {checking ? <Spinner label="Connecting" /> : "Connect"}
          </Button>
        </form>

        <p className="mt-6 text-xs leading-relaxed text-ink-600">
          Nexus uses a single-owner bearer token configured on the backend
          (NEXUS_API_TOKEN). If the API reports that authentication is not
          configured, set the token on the deployment and restart it.
        </p>
      </main>
    </div>
  );
}

export function authFailureMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "AUTH_NOT_CONFIGURED") {
    return "The backend has no authentication configured. Set NEXUS_API_TOKEN on the deployment and restart it.";
  }
  if (error instanceof ApiError && error.status === 401) {
    return "The saved token was rejected. Replace it in Settings.";
  }
  return "Connection failed.";
}

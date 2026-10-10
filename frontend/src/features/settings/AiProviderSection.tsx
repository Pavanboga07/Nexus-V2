import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useAuth } from "../../lib/auth/AuthProvider";
import { describeError } from "../../lib/api/client";
import {
  deleteProviderConfig,
  getProviderStatus,
  saveProviderConfig,
  testProviderConfig,
} from "../../lib/api/endpoints";
import type { ProviderStatus, ProviderTestResult } from "../../lib/api/types";
import { Button } from "../../components/ui/Button";
import { Field, SelectControl, TextInput } from "../../components/ui/Field";
import { Spinner } from "../../components/ui/Feedback";
import { useToast } from "../../components/ui/Toast";

/** Settings → AI Provider: real backend-backed model configuration.
 *
 * The API key travels only inside the authenticated PUT body to the
 * backend, which stores it encrypted; it is never displayed back,
 * never kept in browser storage, and never appears in errors. Test
 * Connection performs one minimal live provider call.
 */
export function AiProviderSection() {
  const { baseUrl, token } = useAuth();
  const { notify } = useToast();
  const [status, setStatus] = useState<ProviderStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [provider, setProvider] = useState("google_genai");
  const [model, setModel] = useState("gemini-2.0-flash");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const loaded = await getProviderStatus(baseUrl, token);
      setStatus(loaded);
      if (loaded.configured) {
        setProvider(loaded.provider ?? "google_genai");
        setModel(loaded.model ?? "gemini-2.0-flash");
      }
    } catch (error) {
      setLoadError(describeError(error));
    } finally {
      setLoading(false);
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onSave = async (event: FormEvent) => {
    event.preventDefault();
    if (provider.trim().length === 0 || model.trim().length === 0) {
      setFormError("Choose a provider and enter a model name.");
      return;
    }
    if (apiKey.trim().length === 0) {
      setFormError("Enter the provider API key.");
      return;
    }
    setFormError(null);
    setSaving(true);
    try {
      const saved = await saveProviderConfig(baseUrl, token, {
        provider: provider.trim(),
        model: model.trim(),
        api_key: apiKey.trim(),
      });
      setStatus(saved);
      setApiKey("");
      setShowKey(false);
      notify("AI provider configuration saved.", "success");
    } catch (error) {
      setFormError(describeError(error));
    } finally {
      setSaving(false);
    }
  };

  const onTest = async () => {
    if (testing) return;
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const result = await testProviderConfig(baseUrl, token, {});
      setTestResult(result);
    } catch (error) {
      setTestError(describeError(error));
    } finally {
      setTesting(false);
    }
  };

  const onRemove = async () => {
    try {
      await deleteProviderConfig(baseUrl, token);
      setStatus({ configured: false, provider: null, model: null, updated_at: null });
      setConfirmingRemove(false);
      notify("AI provider configuration removed.", "info");
    } catch (error) {
      setFormError(describeError(error));
    }
  };

  return (
    <section
      aria-labelledby="ai-provider-heading"
      className="mt-4 rounded-card border border-graphite-700 bg-graphite-900 p-5"
    >
      <h2 id="ai-provider-heading" className="text-base font-semibold text-ink-50">
        AI provider
      </h2>
      {loading ? (
        <p className="mt-2"><Spinner label="Loading provider status" /></p>
      ) : null}
      {loadError && !loading ? (
        <p role="alert" className="mt-2 text-sm text-signal-red">
          {loadError}{" "}
          <button
            type="button"
            onClick={() => void refresh()}
            className="cursor-pointer underline hover:text-ink-100"
          >
            Retry
          </button>
        </p>
      ) : null}
      {!loading && !loadError && status ? (
        <p className="mt-1 text-sm text-ink-500" role="status">
          {status.configured
            ? `Configured: ${status.provider} / ${status.model}. The key is stored encrypted and never shown.`
            : "No provider configured. Chat and remote execution need one."}
        </p>
      ) : null}

      <form onSubmit={onSave} className="mt-4 flex flex-col gap-3">
        <Field label="Provider">
          <SelectControl
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
          >
            <option value="google_genai">Google Gemini</option>
          </SelectControl>
        </Field>
        <Field
          label="Model"
          hint="Provider model id, e.g. gemini-2.0-flash."
        >
          <TextInput
            value={model}
            onChange={(event) => setModel(event.target.value)}
            spellCheck={false}
            autoComplete="off"
          />
        </Field>
        <Field
          label={status?.configured ? "Replace API key" : "API key"}
          hint="Sent to your backend only, stored encrypted. Never shown again."
        >
          <div className="flex gap-2">
            <TextInput
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              type={showKey ? "text" : "password"}
              autoComplete="new-password"
              spellCheck={false}
              aria-label={status?.configured ? "Replace API key" : "API key"}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setShowKey((shown) => !shown)}
              aria-pressed={showKey}
            >
              {showKey ? "Hide" : "Show"}
            </Button>
          </div>
        </Field>
        {formError ? (
          <p role="alert" className="text-sm text-signal-red">{formError}</p>
        ) : null}
        <div className="flex flex-col-reverse gap-2 sm:flex-row">
          <Button variant="secondary" size="sm" type="submit" busy={saving}>
            {saving ? <Spinner label="Saving" /> : "Save configuration"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onTest}
            busy={testing}
            disabled={!status?.configured}
            title={
              status?.configured
                ? "Run one minimal live call with the saved configuration"
                : "Save a configuration first"
            }
          >
            {testing ? <Spinner label="Testing" /> : "Test connection"}
          </Button>
          {status?.configured ? (
            confirmingRemove ? (
              <>
                <Button variant="ghost" size="sm" onClick={() => setConfirmingRemove(false)}>
                  Keep it
                </Button>
                <Button variant="danger" size="sm" onClick={() => void onRemove()}>
                  Confirm remove
                </Button>
              </>
            ) : (
              <Button variant="ghost" size="sm" onClick={() => setConfirmingRemove(true)}>
                Remove
              </Button>
            )
          ) : null}
        </div>
      </form>

      {testResult ? (
        <p role="status" className="mt-3 text-sm text-signal-green">
          Connected to {testResult.provider} / {testResult.model} in{" "}
          {testResult.latency_ms} ms. Reply: {testResult.reply_preview}
        </p>
      ) : null}
      {testError ? (
        <p role="alert" className="mt-3 text-sm text-signal-red">{testError}</p>
      ) : null}
      <p className="mt-3 text-xs leading-relaxed text-ink-600">
        Without a configured provider, chat turns and remote execution
        report that no model is configured. Environment-based settings
        (`NEXUS_LLM_MODEL`) keep working as a fallback when nothing is
        saved here.
      </p>
    </section>
  );
}

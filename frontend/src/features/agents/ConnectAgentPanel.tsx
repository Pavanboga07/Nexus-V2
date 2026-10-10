import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../../lib/auth/AuthProvider";
import { useEventBus } from "../../app/EventBus";
import { describeError } from "../../lib/api/client";
import {
  acceptPairing,
  createInvitation,
  listInvitations,
  listPairings,
  revokeInvitation,
  validateInvitation,
} from "../../lib/api/endpoints";
import type {
  Agent,
  InvitationInfo,
  InvitationRow,
  Pairing,
} from "../../lib/api/types";
import { Button } from "../../components/ui/Button";
import { Field, SelectControl, TextArea } from "../../components/ui/Field";
import { Spinner, StatusBadge } from "../../components/ui/Feedback";
import { useToast } from "../../components/ui/Toast";
import type { StatusTone } from "../../components/ui/Feedback";

function pairingTone(status: string): StatusTone {
  if (status === "completed") return "ok";
  if (status === "rejected" || status === "expired") return "bad";
  return "pending";
}

/** Connect Agent: invite partners and accept invitations.
 *
 * Invitations authorize first contact out-of-band (signed envelope +
 * single-use secret). Accepting sends a real pairing request through
 * the Gateway; the remote owner approves it in their approvals inbox
 * and completes the pairing, which arrives here as a signed
 * acceptance. Nothing shows "Connected" before that round trip.
 */
export function ConnectAgentPanel({ agents, onChanged }: {
  agents: Agent[];
  onChanged: () => void;
}) {
  const { baseUrl, token } = useAuth();
  const { subscribe } = useEventBus();
  const { notify } = useToast();
  const [tab, setTab] = useState<"invite" | "accept">("invite");
  const [agentId, setAgentId] = useState(agents[0]?.agent_id ?? "");
  const current = agentId || agents[0]?.agent_id || "";

  const [invitations, setInvitations] = useState<InvitationRow[]>([]);
  const [links, setLinks] = useState<Pairing[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Invite tab state.
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<{
    invitation: string;
    expires_at: string;
  } | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  // Accept tab state.
  const [code, setCode] = useState("");
  const [preview, setPreview] = useState<InvitationInfo | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateError, setValidateError] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!current) return;
    setLoading(true);
    setError(null);
    try {
      const [listedInvitations, listedLinks] = await Promise.all([
        listInvitations(baseUrl, token, current),
        listPairings(baseUrl, token, current),
      ]);
      setInvitations(listedInvitations);
      setLinks(listedLinks);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setLoading(false);
    }
  }, [baseUrl, token, current]);

  useEffect(() => {
    setCreated(null);
    setPreview(null);
    void reload();
  }, [reload]);

  useEffect(() => subscribe("agents", () => void reload()), [subscribe, reload]);

  // Poll open links while any is pending (acceptance arrives async).
  useEffect(() => {
    if (!links.some((link) => link.status === "pending")) return;
    const timer = window.setInterval(() => void reload(), 3000);
    return () => window.clearInterval(timer);
  }, [links, reload]);

  const onCreate = async (event: FormEvent) => {
    event.preventDefault();
    if (!current || creating) return;
    setCreating(true);
    setCreated(null);
    try {
      const made = await createInvitation(baseUrl, token, { agent_id: current });
      setCreated({ invitation: made.invitation, expires_at: made.expires_at });
      await reload();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setCreating(false);
    }
  };

  const onRevokeInvitation = async (invitationId: string) => {
    setRevoking(invitationId);
    try {
      await revokeInvitation(baseUrl, token, invitationId);
      notify("Invitation revoked. It cannot be used for new pairings.", "info");
      await reload();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setRevoking(null);
    }
  };

  const onValidate = async (event: FormEvent) => {
    event.preventDefault();
    if (code.trim().length === 0 || validating) return;
    setValidating(true);
    setValidateError(null);
    setPreview(null);
    try {
      setPreview(await validateInvitation(baseUrl, token, code.trim()));
    } catch (caught) {
      setValidateError(describeError(caught));
    } finally {
      setValidating(false);
    }
  };

  const onAccept = async () => {
    if (!current || accepting) return;
    setAccepting(true);
    setAcceptError(null);
    try {
      const link = await acceptPairing(baseUrl, token, {
        invitation: code.trim(),
        sender_agent_id: current,
      });
      setCode("");
      setPreview(null);
      notify(`Pairing request ${link.request_id} sent. Awaiting remote approval.`, "info");
      await reload();
      onChanged();
    } catch (caught) {
      setAcceptError(describeError(caught));
    } finally {
      setAccepting(false);
    }
  };

  return (
    <section
      aria-labelledby="connect-heading"
      className="mb-6 rounded-card border border-graphite-700 bg-graphite-900 p-5"
    >
      <h2 id="connect-heading" className="text-base font-semibold text-ink-50">
        Connect a new agent
      </h2>
      <p className="mt-1 text-sm text-ink-500">
        Pair with another person's Nexus over the Gateway. Invitations
        prove who you are dealing with before anything is trusted.
      </p>

      <div className="mt-3 flex flex-col gap-2">
        <Field label="Acting agent">
          <SelectControl
            value={current}
            onChange={(event) => setAgentId(event.target.value)}
            aria-label="Acting agent"
          >
            {agents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>
                {agent.agent_id}
              </option>
            ))}
          </SelectControl>
        </Field>
        <div role="tablist" aria-label="Connect mode" className="flex gap-1 rounded-control border border-graphite-700 bg-graphite-950 p-1">
          {(["invite", "accept"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              role="tab"
              aria-selected={tab === mode}
              onClick={() => setTab(mode)}
              className={
                tab === mode
                  ? "min-h-[40px] flex-1 cursor-pointer rounded-[6px] bg-graphite-700 px-3 text-sm font-medium text-ink-50"
                  : "min-h-[40px] flex-1 cursor-pointer rounded-[6px] px-3 text-sm font-medium text-ink-500 hover:text-ink-100"
              }
            >
              {mode === "invite" ? "Invite a partner" : "Accept invitation"}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-signal-red">{error}</p>
      ) : null}

      {tab === "invite" ? (
        <div className="mt-4 flex flex-col gap-3">
          <form onSubmit={onCreate}>
            <Button variant="primary" size="sm" type="submit" busy={creating} disabled={!current}>
              {creating ? <Spinner label="Creating" /> : "Create invitation"}
            </Button>
          </form>
          {created ? (
            <div className="rounded-control border border-accent-500/40 bg-graphite-950 p-3">
              <p className="text-sm font-medium text-ink-50">
                Share this invitation code (shown once). Expires {created.expires_at}.
              </p>
              <label htmlFor="invitation-code" className="sr-only">
                Invitation code
              </label>
              <TextArea
                id="invitation-code"
                readOnly
                value={created.invitation}
                rows={3}
                className="mt-2 font-mono text-xs"
              />
              <div className="mt-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    void navigator.clipboard?.writeText(created.invitation).catch(() => {
                      notify("Copy failed. Select the code manually.", "error");
                    });
                  }}
                >
                  Copy code
                </Button>
              </div>
            </div>
          ) : null}
          <div>
            <h3 className="text-[13px] font-medium text-ink-300">
              Issued invitations ({invitations.length})
            </h3>
            {loading ? <p className="mt-1"><Spinner label="Loading invitations" /></p> : null}
            <ul className="mt-2 flex flex-col gap-1.5">
              {invitations.map((row) => (
                <li
                  key={row.invitation_id}
                  className="flex flex-wrap items-center gap-2 rounded-control bg-graphite-950 px-3 py-2 text-sm"
                >
                  <span className="font-mono text-xs text-ink-300">
                    {row.invitation_id.slice(0, 20)}...
                  </span>
                  <StatusBadge
                    tone={row.revoked ? "bad" : row.used ? "neutral" : "pending"}
                    label={row.revoked ? "Revoked" : row.used ? "Used" : "Open"}
                  />
                  {!row.revoked && !row.used ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      busy={revoking === row.invitation_id}
                      onClick={() => void onRevokeInvitation(row.invitation_id)}
                    >
                      Revoke
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
            {!loading && invitations.length === 0 ? (
              <p className="mt-1 text-sm text-ink-500">No invitations issued yet.</p>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-3">
          <form onSubmit={onValidate} className="flex flex-col gap-3">
            <Field
              label="Invitation code"
              hint="Paste the code your partner shared. Nothing is trusted until it validates."
            >
              <TextArea
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder="inv.…"
                rows={3}
                className="font-mono text-xs"
              />
            </Field>
            <div>
              <Button variant="secondary" size="sm" type="submit" busy={validating}>
                {validating ? <Spinner label="Validating" /> : "Validate invitation"}
              </Button>
            </div>
          </form>
          {validateError ? (
            <p role="alert" className="text-sm text-signal-red">{validateError}</p>
          ) : null}
          {preview ? (
            <div className="rounded-control border border-graphite-700 bg-graphite-950 p-3 text-sm">
              <p className="font-medium text-ink-50">Invitation checks out. Verify out-of-band that this is really them:</p>
              <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-ink-500">Agent</dt>
                  <dd className="break-all font-mono text-ink-100">{preview.agent_id}</dd>
                </div>
                <div>
                  <dt className="text-xs text-ink-500">Key fingerprint</dt>
                  <dd className="font-mono text-ink-100">{preview.fingerprint}</dd>
                </div>
                <div>
                  <dt className="text-xs text-ink-500">Expires</dt>
                  <dd className="text-ink-100">{preview.expires_at}</dd>
                </div>
              </dl>
              {acceptError ? (
                <p role="alert" className="mt-2 text-sm text-signal-red">{acceptError}</p>
              ) : null}
              <div className="mt-3">
                <Button variant="primary" size="sm" onClick={onAccept} busy={accepting}>
                  {accepting ? <Spinner label="Sending" /> : "Send pairing request"}
                </Button>
              </div>
            </div>
          ) : null}
          <div>
            <h3 className="text-[13px] font-medium text-ink-300">
              Pairings ({links.length})
            </h3>
            <ul className="mt-2 flex flex-col gap-1.5">
              {links.map((link) => (
                <li
                  key={link.pairing_id}
                  className="flex flex-wrap items-center gap-2 rounded-control bg-graphite-950 px-3 py-2 text-sm"
                >
                  <span className="truncate font-mono text-xs text-ink-100">
                    {link.remote_agent_id}
                  </span>
                  <StatusBadge tone={pairingTone(link.status)} label={link.status} />
                  {link.request_id ? (
                    <Link
                      to={`/requests/${encodeURIComponent(link.request_id)}`}
                      className="text-xs font-medium text-accent-300 hover:text-accent-400"
                    >
                      View request
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
            {!loading && links.length === 0 ? (
              <p className="mt-1 text-sm text-ink-500">
                No pairings yet. Accept an invitation above; the link turns
                completed only after the partner approves and their signed
                acceptance arrives.
              </p>
            ) : null}
          </div>
        </div>
      )}
      <p className="mt-3 text-xs leading-relaxed text-ink-600">
        Revoking an invitation blocks new uses; already-decided
        pairings are unaffected (remove the peer below to end those).
        To remove an existing peer, use the Revoke control beside it
        in the agent list.
      </p>
    </section>
  );
}

import { expect, test } from "@playwright/test";
import { E2E_TOKEN } from "../playwright.config";
import { signIn } from "./helpers";

test.setTimeout(90000);

/** Real ticket + subprotocol WebSocket flow against the live backend.
 *  Runs in the page so the browser's actual WebSocket implementation
 *  performs the handshake. */
test("subprotocol ticket connects and receives request.created", async ({ page }) => {
  await signIn(page);

  const result = await page.evaluate(async (token) => {
    const ticketResponse = await fetch("/api/v1/events/ticket", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!ticketResponse.ok) return { ok: false, stage: "ticket" };
    const { ticket } = (await ticketResponse.json()) as { ticket: string };
    const socket = new WebSocket(
      `${location.origin.replace("http", "ws")}/api/v1/events/ws`,
      [`nexus-ticket.${ticket}`, "nexus-events"],
    );
    const opened = await new Promise<unknown>((resolve) => {
      const timer = setTimeout(
        () =>
          resolve({
            opened: false,
            readyState: socket.readyState,
            protocol: socket.protocol,
          }),
        8000,
      );
      socket.onopen = () => {
        clearTimeout(timer);
        resolve({ opened: true, protocol: socket.protocol });
      };
      socket.onerror = () => {
        clearTimeout(timer);
        resolve({ opened: false, readyState: socket.readyState });
      };
    });
    if (!(opened as { opened: boolean }).opened)
      return { ok: false, stage: "open", detail: opened };
    // Look up real participants first (ownership-gated endpoints).
    const agentsResponse = await fetch("/api/v1/agents", {
      headers: { Authorization: `Bearer ${token}` },
    });
    const agents = (await agentsResponse.json()) as Array<{ agent_id: string }>;
    const peersResponse = await fetch(`/api/v1/agents/${agents[0].agent_id}/peers`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const peers = (await peersResponse.json()) as Array<{ peer_agent_id: string }>;
    const log: string[] = [];
    const stamp = (message: string) =>
      log.push(`${Math.round(performance.now())}ms ${message}`);
    // The event may legitimately arrive before the POST response
    // completes (publish precedes response serialization), so wait for
    // both the event and the settled POST status before returning.
    const deadline = Date.now() + 15000;
    let receivedEvent: unknown = null;
    const received = await new Promise<unknown>((resolve) => {
      const timer = setTimeout(() => {
        stamp("timer-fired");
        resolve(null);
      }, 15000);
      socket.onmessage = (event: MessageEvent) => {
        try {
          const parsed = JSON.parse(String(event.data)) as { type?: string };
          stamp(`message ${parsed.type ?? "unknown"}`);
          if (parsed.type === "remote.request.created") {
            receivedEvent = parsed;
          }
        } catch {
          // Malformed frames are ignored by design.
        }
      };
      const poll = () => {
        const status = (
          window as unknown as { __wsProbeStatus?: number }
        ).__wsProbeStatus;
        if (receivedEvent !== null && status !== undefined) {
          clearTimeout(timer);
          resolve(receivedEvent);
        } else if (Date.now() > deadline) {
          clearTimeout(timer);
          resolve(receivedEvent);
        } else {
          setTimeout(poll, 100);
        }
      };
      // Trigger a real event from the page: create a request via REST,
      // retrying once against transient stalls under suite load.
      void (async () => {
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            stamp(`post-attempt-${attempt}`);
            const created = await Promise.race([
              fetch("/api/v1/requests", {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  Authorization: `Bearer ${token}`,
                },
                body: JSON.stringify({
                  sender_agent_id: agents[0].agent_id,
                  recipient_agent_id: peers[0].peer_agent_id,
                  query: "ws probe",
                }),
              }),
              new Promise<never>((_, reject) =>
                setTimeout(() => reject(new Error("create timeout")), 8000),
              ),
            ]);
            (window as unknown as { __wsProbeStatus?: number }).__wsProbeStatus =
              created.status;
            stamp(`post-status-${created.status}`);
            if (created.status === 201) return;
          } catch {
            (window as unknown as { __wsProbeStatus?: number }).__wsProbeStatus = -1;
            stamp("post-error");
          }
        }
      })();
      setTimeout(poll, 100);
    });
    socket.close();
    const createStatus = (
      window as unknown as { __wsProbeStatus?: number }
    ).__wsProbeStatus;
    return { ok: received !== null, stage: "event", received, createStatus, log };
  }, E2E_TOKEN);

  // eslint-disable-next-line no-console
  console.log("WS RESULT:", JSON.stringify(result).slice(0, 900));
  // The agents endpoint gates on ownership; use the real local agent id.
  expect(result.createStatus).toBe(201);
  expect(result.ok).toBe(true);
  expect(JSON.stringify(result.received)).toContain("remote.request.created");
});

test("query-string ticket is rejected with 4401", async ({ page }) => {
  await signIn(page);
  const code = await page.evaluate(async (token) => {
    const ticketResponse = await fetch("/api/v1/events/ticket", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    const { ticket } = (await ticketResponse.json()) as { ticket: string };
    const socket = new WebSocket(
      `${location.origin.replace("http", "ws")}/api/v1/events/ws?ticket=${ticket}`,
    );
    return await new Promise<number | null>((resolve) => {
      socket.onclose = (event: CloseEvent) => resolve(event.code);
      socket.onerror = () => resolve(null);
      setTimeout(() => resolve(null), 8000);
    });
  }, E2E_TOKEN);
  expect(code).toBe(4401);
});

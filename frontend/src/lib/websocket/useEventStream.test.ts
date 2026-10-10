import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { FakeWebSocket } from "../../test/setup";
import { TEST_BASE, TEST_TOKEN, stubFetch } from "../../test/helpers";
import {
  NEGOTIATED_SUBPROTOCOL,
  TICKET_SUBPROTOCOL_PREFIX,
  __wsInternals,
  useEventStream,
} from "./useEventStream";

function ticketRoute(ticket = "ticket-abc") {
  return stubFetch({
    "POST /api/v1/events/ticket": { body: { ticket, expires_at: "2026-10-10T00:01:00Z" } },
  });
}

describe("useEventStream", () => {
  it("opens with the ticket in the subprotocol, never the URL", async () => {
    ticketRoute();
    const { result, unmount } = renderHook(() =>
      useEventStream({ baseUrl: TEST_BASE, token: TEST_TOKEN, enabled: true }),
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    expect(socket.url).toBe("ws://localhost:5173/api/v1/events/ws");
    expect(socket.url).not.toContain("ticket-abc");
    expect(socket.url).not.toContain(TEST_TOKEN);
    expect(socket.protocol).toEqual([
      `${TICKET_SUBPROTOCOL_PREFIX}ticket-abc`,
      NEGOTIATED_SUBPROTOCOL,
    ]);
    act(() => socket.serverOpen());
    expect(result.current.state).toBe("connected");
    unmount();
  });

  it("ignores malformed envelopes and reports each event once", async () => {
    ticketRoute();
    const seen: string[] = [];
    const { unmount } = renderHook(() =>
      useEventStream({
        baseUrl: TEST_BASE,
        token: TEST_TOKEN,
        enabled: true,
        onEvent: (item) => {
          if (!item.duplicate) seen.push(item.event.event_id);
        },
      }),
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.serverOpen();
      socket.serverMessage({ nope: true });
      socket.serverMessage("just a string");
      socket.serverMessage({
        event_id: "evt-1",
        type: "remote.request.updated",
        timestamp: "2026-10-10T00:00:00Z",
        data: { request_id: "r1", status: "APPROVED" },
      });
      socket.serverMessage({
        event_id: "evt-1",
        type: "remote.request.updated",
        timestamp: "2026-10-10T00:00:00Z",
        data: { request_id: "r1", status: "APPROVED" },
      });
    });
    expect(seen).toEqual(["evt-1"]);
    unmount();
  });

  it("reconnects with a fresh ticket after a drop and resyncs", async () => {
    const { calls } = ticketRoute();
    let syncs = 0;
    const { unmount } = renderHook(() =>
      useEventStream({
        baseUrl: TEST_BASE,
        token: TEST_TOKEN,
        enabled: true,
        onSyncNeeded: () => {
          syncs += 1;
        },
      }),
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    act(() => {
      FakeWebSocket.instances[0].serverOpen();
    });
    // Switch to fake timers only for the backoff window.
    vi.useFakeTimers();
    try {
      act(() => {
        FakeWebSocket.instances[0].serverClose();
      });
      // First retry after ~1s backoff fetches a new ticket.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200);
      });
      expect(
        calls.filter((call) => call.url.endsWith("/api/v1/events/ticket")),
      ).toHaveLength(2);
      expect(FakeWebSocket.instances).toHaveLength(2);
      act(() => FakeWebSocket.instances[1].serverOpen());
      expect(syncs).toBe(1);
    } finally {
      vi.useRealTimers();
    }
    unmount();
  });

  it("closes the socket and stops timers on unmount", async () => {
    ticketRoute();
    const { unmount } = renderHook(() =>
      useEventStream({ baseUrl: TEST_BASE, token: TEST_TOKEN, enabled: true }),
    );
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    unmount();
    expect(socket.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("stays idle without a token", () => {
    const { result, unmount } = renderHook(() =>
      useEventStream({ baseUrl: TEST_BASE, token: "", enabled: true }),
    );
    expect(result.current.state).toBe("idle");
    expect(FakeWebSocket.instances).toHaveLength(0);
    unmount();
  });

  it("validates envelopes strictly", () => {
    expect(
      __wsInternals.isValidEnvelope({
        event_id: "e",
        type: "t",
        timestamp: "ts",
        data: {},
      }),
    ).toBe(true);
    expect(__wsInternals.isValidEnvelope({ event_id: "e", type: "t" })).toBe(false);
    expect(
      __wsInternals.isValidEnvelope({
        event_id: "e",
        type: "t",
        timestamp: "ts",
        data: null,
      }),
    ).toBe(false);
    expect(__wsInternals.wsUrlFor("https://example.com")).toBe(
      "wss://example.com/api/v1/events/ws",
    );
  });
});

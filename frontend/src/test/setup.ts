import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

/** Controllable WebSocket double. Real handshake semantics (subprotocol
 *  negotiation, close codes) are covered by backend tests and the live
 *  origin probe; here we verify our lifecycle: URL shape, protocol
 *  header, envelope handling, and cleanup. */
export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readonly protocol: string | string[];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  closed = false;

  constructor(url: string, protocol: string | string[] = []) {
    this.url = url;
    this.protocol = protocol;
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({});
  }

  serverOpen(): void {
    this.onopen?.({});
  }

  serverMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  serverClose(): void {
    this.onclose?.({});
  }
}

vi.stubGlobal("WebSocket", FakeWebSocket);

beforeEach(() => {
  FakeWebSocket.instances = [];
  sessionStorage.clear();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  cleanup();
});

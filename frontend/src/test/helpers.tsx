import { render, type RenderOptions } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../lib/auth/AuthProvider";
import { ToastProvider } from "../components/ui/Toast";
import { EventProvider } from "../app/EventBus";

export const TEST_TOKEN = "test-token-123";
export const TEST_BASE = "http://localhost:5173";

export function seedAuth(token: string = TEST_TOKEN, base: string = TEST_BASE): void {
  sessionStorage.setItem("nexus.api.token", token);
  try {
    localStorage.setItem("nexus.api.baseUrl", base);
  } catch {
    // Storage may be unavailable in some runners.
  }
}

/** Full provider stack used by feature tests. */
export function renderApp(
  ui: ReactElement,
  options: RenderOptions & { route?: string } = {},
) {
  const { route = "/", ...rest } = options;
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <ToastProvider>
          <EventProvider>{ui}</EventProvider>
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
    rest,
  );
}

export interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
  auth: string | null;
}

/** Fetch stub keyed by "METHOD path". Unmatched calls 404 honestly. */
export function stubFetch(
  routes: Record<string, { status?: number; body?: unknown }>,
): { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const stub = async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    let body: unknown = null;
    if (typeof init?.body === "string" && init.body.length > 0) {
      try {
        body = JSON.parse(init.body) as unknown;
      } catch {
        body = init.body;
      }
    }
    calls.push({ url, method, body, auth: headers.get("Authorization") });
    const path = new URL(url, TEST_BASE).pathname;
    const route = routes[`${method} ${path}`];
    if (!route) {
      return new Response(JSON.stringify({ code: "NOT_FOUND", message: "request failed" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify(route.body ?? null), {
      status: route.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  vi.stubGlobal("fetch", stub);
  return { calls };
}

// Re-exported so test files import from one place.
import { vi } from "vitest";
export { vi };

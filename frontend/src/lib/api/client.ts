import type { ApiErrorBody } from "./types";

/** Safe, typed error for every API failure. Carries only the backend's
 *  static code/message pair, never tokens or payloads. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  get isAuth(): boolean {
    return this.status === 401 || this.code === "AUTH_NOT_CONFIGURED";
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}

export interface RequestOptions {
  signal?: AbortSignal;
}

function errorMessageFor(status: number, body: unknown): ApiErrorBody {
  if (body !== null && typeof body === "object") {
    const record = body as Record<string, unknown>;
    if (typeof record.code === "string" && typeof record.message === "string") {
      return { code: record.code, message: record.message };
    }
    // Health/readiness uses a bare {status} shape instead of the envelope.
    if (typeof record.status === "string") {
      return {
        code: status === 200 ? "OK" : "NOT_READY",
        message: `Service reports status: ${record.status}`,
      };
    }
  }
  if (status === 0) {
    return {
      code: "NETWORK_ERROR",
      message: "The Nexus backend could not be reached.",
    };
  }
  return { code: "HTTP_ERROR", message: "The request failed." };
}

/** Minimal typed HTTP client for the Nexus REST API. The bearer token is
 *  supplied per call from session storage; nothing here retains it. */
export async function request<T>(
  baseUrl: string,
  token: string,
  path: string,
  init: RequestInit & RequestOptions = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/api/v1${path}`, {
      ...init,
      signal: init.signal,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(init.headers ?? {}),
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    const fallback = errorMessageFor(0, null);
    throw new ApiError(0, fallback.code, fallback.message);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  let body: unknown = null;
  const text = await response.text();
  if (text.length > 0) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const parsed = errorMessageFor(response.status, body);
    throw new ApiError(response.status, parsed.code, parsed.message);
  }
  return body as T;
}

/** Unauthenticated GET used for the public readiness probe. */
export async function publicGet<T>(baseUrl: string, path: string): Promise<T> {
  const response = await fetch(`${baseUrl}/api/v1${path}`);
  const text = await response.text();
  const body = text.length > 0 ? (JSON.parse(text) as unknown) : null;
  if (!response.ok) {
    const parsed = errorMessageFor(response.status, body);
    throw new ApiError(response.status, parsed.code, parsed.message);
  }
  return body as T;
}

/** Friendly one-line summary for error display. Never includes secrets:
 *  ApiError messages are backend-static strings by construction. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "The request was cancelled.";
  }
  return "Something went wrong. Try again.";
}

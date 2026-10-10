import { describe, expect, it } from "vitest";
import { ApiError, describeError, request } from "./client";
import { TEST_BASE, TEST_TOKEN, stubFetch } from "../../test/helpers";

describe("api client", () => {
  it("sends the bearer token as a header, never in the URL", async () => {
    const { calls } = stubFetch({ "GET /api/v1/agents": { body: [] } });
    await request<unknown[]>(TEST_BASE, TEST_TOKEN, "/agents");
    expect(calls).toHaveLength(1);
    expect(calls[0].auth).toBe(`Bearer ${TEST_TOKEN}`);
    expect(calls[0].url).not.toContain(TEST_TOKEN);
  });

  it("maps backend error envelopes to safe ApiErrors", async () => {
    stubFetch({
      "GET /api/v1/agents": {
        status: 401,
        body: { code: "UNAUTHORIZED", message: "authentication required" },
      },
    });
    const error = await request<unknown[]>(TEST_BASE, "bad", "/agents").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).code).toBe("UNAUTHORIZED");
    expect(describeError(error)).toBe("authentication required");
  });

  it("turns unreachable backends into a safe network error", async () => {
    const { vi: vitest } = await import("../../test/helpers");
    vitest.stubGlobal(
      "fetch",
      () => Promise.reject(new TypeError("fetch failed")),
    );
    const error = await request<unknown[]>(TEST_BASE, TEST_TOKEN, "/agents").catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("NETWORK_ERROR");
  });
});

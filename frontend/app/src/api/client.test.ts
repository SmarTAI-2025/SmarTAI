import { AxiosError, type AxiosAdapter } from "axios";
import { afterEach, describe, expect, it } from "vitest";
import {
  APIError,
  apiClient,
  clearAuthToken,
  getAuthToken,
  getAPIErrorCode,
  getAPIErrorDetail,
  normalizeAPIError,
  setAuthToken,
} from "./client";

function axiosError(data: unknown, status = 409, headers: Record<string, string> = {}) {
  return {
    isAxiosError: true,
    message: "Request failed",
    config: {},
    response: {
      data,
      status,
      statusText: "Conflict",
      headers,
      config: {},
    },
  };
}

describe("API error envelope compatibility", () => {
  it.each([
    [{ error: { code: "domain_conflict", message: "Domain conflict" } }, "domain_conflict"],
    [{ detail: { code: "fastapi_conflict", message: "FastAPI conflict" } }, "fastapi_conflict"],
    [{ code: "top_level_conflict", message: "Top-level conflict" }, "top_level_conflict"],
  ])("reads the stable code from %j", (payload, code) => {
    const error = new APIError(409, "Conflict", payload);

    expect(getAPIErrorCode(error)).toBe(code);
    expect(getAPIErrorDetail(error)?.code).toBe(code);
  });

  it("uses the DomainError envelope message when normalizing an Axios error", () => {
    const error = normalizeAPIError(axiosError({
      error: { code: "workflow_revision_conflict", message: "Reload the task." },
    }));

    expect(error.status).toBe(409);
    expect(error.message).toBe("Reload the task.");
    expect(getAPIErrorCode(error)).toBe("workflow_revision_conflict");
  });

  it("reads retry metadata from any supported object envelope", () => {
    const error = normalizeAPIError(axiosError({
      error: { code: "rate_limited", retry_after_seconds: 2.2 },
    }, 429));

    expect(error.retryAfterSeconds).toBe(3);
  });
});

describe("Demo token renewal through the existing refresh interceptor", () => {
  const originalAdapter = apiClient.defaults.adapter;
  afterEach(() => {
    apiClient.defaults.adapter = originalAdapter;
    clearAuthToken();
  });

  it("shares one renewal and replays concurrent task requests with the renewed token", async () => {
    setAuthToken("expired-demo-capability");
    const refreshTokens: unknown[] = [];
    const replayedPaths: string[] = [];
    const adapter: AxiosAdapter = async (config) => {
      const response = { data: {}, status: 200, statusText: "OK", headers: {}, config };
      if (config.url === "/auth/refresh") {
        refreshTokens.push(config.headers.Authorization);
        return { ...response, data: { token: "renewed-same-owner-capability" } };
      }
      if (config.headers.Authorization === "Bearer expired-demo-capability") {
        throw new AxiosError("expired", "ERR_BAD_REQUEST", config, undefined, { ...response, status: 401 });
      }
      expect(config.headers.Authorization).toBe("Bearer renewed-same-owner-capability");
      replayedPaths.push(config.url!);
      return response;
    };
    apiClient.defaults.adapter = adapter;

    await Promise.all([
      apiClient.post("/analytics/synthetic-task/filter-intent", { query: "sort by name" }),
      apiClient.get("/tasks/synthetic-task"),
    ]);

    expect(refreshTokens).toEqual(["Bearer expired-demo-capability"]);
    expect(replayedPaths.sort()).toEqual(["/analytics/synthetic-task/filter-intent", "/tasks/synthetic-task"]);
    expect(getAuthToken()).toBe("renewed-same-owner-capability");
  });

  it("stops after a refused renewal instead of issuing a new demo identity", async () => {
    setAuthToken("expired-demo-capability");
    const paths: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      paths.push(config.url!);
      throw new AxiosError("expired", "ERR_BAD_REQUEST", config, undefined, {
        data: { detail: { code: "frontier_demo_session_expired" } },
        status: 401, statusText: "Unauthorized", headers: {}, config,
      });
    };

    await expect(apiClient.get("/tasks/synthetic-task")).rejects.toBeInstanceOf(AxiosError);

    expect(paths).toEqual(["/tasks/synthetic-task", "/auth/refresh"]);
    expect(getAuthToken()).toBeNull();
  });
});

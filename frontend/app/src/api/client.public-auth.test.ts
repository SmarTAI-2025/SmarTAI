import { AxiosError, AxiosHeaders } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  apiClient,
  normalizeAPIError,
  SMARTAI_TOKEN_STORAGE_KEY,
  type AuthAwareRequestConfig,
} from "@/api/client";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("public auth interceptor boundary", () => {
  it("does not attach stale auth, refresh a 401, or retry the public request", async () => {
    window.localStorage.setItem(SMARTAI_TOKEN_STORAGE_KEY, "stale-access-token");
    let authorization: unknown;
    const adapter = vi.fn(async (config) => {
      authorization = config.headers.get("Authorization");
      const response = {
        data: { detail: { code: "registration_unavailable" } },
        status: 401,
        statusText: "Unauthorized",
        headers: new AxiosHeaders(),
        config,
      };
      throw new AxiosError(
        "Request failed with status code 401",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
      );
    });
    const refresh = vi.spyOn(apiClient, "post").mockRejectedValue(new Error("refresh must not run"));

    await expect(apiClient.request({
      method: "POST",
      url: "/auth/register/request",
      data: {},
      adapter,
      _skipAuthHeader: true,
      _skipAuthRefresh: true,
    } as AuthAwareRequestConfig)).rejects.toMatchObject({ response: { status: 401 } });

    expect(adapter).toHaveBeenCalledTimes(1);
    expect(authorization).toBeUndefined();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("reads Retry-After case-insensitively from AxiosHeaders and plain headers", () => {
    const config = { headers: new AxiosHeaders() };
    const response = {
      data: { detail: { code: "registration_rate_limited" } },
      status: 429,
      statusText: "Too Many Requests",
      headers: new AxiosHeaders({ "Retry-After": "3" }),
      config,
    };
    const axiosError = new AxiosError(
      "rate limited",
      AxiosError.ERR_BAD_REQUEST,
      config,
      undefined,
      response,
    );
    expect(normalizeAPIError(axiosError).retryAfterSeconds).toBe(3);

    response.headers = { "Retry-After": "4" } as unknown as AxiosHeaders;
    expect(normalizeAPIError(axiosError).retryAfterSeconds).toBe(4);
  });
});

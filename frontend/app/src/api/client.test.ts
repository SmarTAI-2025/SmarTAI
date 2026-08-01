import { describe, expect, it } from "vitest";
import {
  APIError,
  getAPIErrorCode,
  getAPIErrorDetail,
  normalizeAPIError,
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

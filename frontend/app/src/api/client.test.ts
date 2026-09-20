import { afterEach, describe, expect, it, vi } from "vitest";
import {
  APIError,
  apiClient,
  getAPIErrorCode,
  getAPIErrorDetail,
  getBlob,
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
  afterEach(() => {
    vi.restoreAllMocks();
  });

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

  it.each([
    [409, "source_cleanup_pending"],
    [410, "source_unavailable_task_finalized"],
    [404, "source_unavailable_missing"],
  ])("decodes a JSON error Blob at HTTP %i and retains %s", async (status, code) => {
    vi.spyOn(apiClient, "get").mockRejectedValue(axiosError(
      new Blob([
        JSON.stringify({
          error: {
            code,
            message: "Automatic cleanup is in progress.",
          },
        }),
      ], { type: "application/json" }),
      status,
    ));

    const error = await getBlob("/tasks/task-1/source-files/file-1/content")
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(APIError);
    expect(error).toMatchObject({ status, message: "Automatic cleanup is in progress." });
    expect(getAPIErrorCode(error)).toBe(code);
  });
});

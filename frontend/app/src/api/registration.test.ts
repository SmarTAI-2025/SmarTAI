import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError, postJSON } from "@/api/client";
import {
  temporaryVerificationPath,
  verifyRegistrationAdapter,
} from "./registration.adapter";
import {
  requestRegistration,
  shouldUseTemporaryRegistrationAdapter,
} from "./registration";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, postJSON: vi.fn() };
});

const request = {
  username: "teacher",
  email: "teacher@example.edu",
  password: "safe-password",
};

describe("registration client temporary adapter boundary", () => {
  beforeEach(() => {
    vi.mocked(postJSON).mockReset();
  });

  it("prefers a successful real registration API response", async () => {
    vi.mocked(postJSON).mockResolvedValue({
      status: "verification_required",
      request_id: "api-request",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });

    await expect(requestRegistration(request)).resolves.toMatchObject({
      request_id: "api-request",
      transport: "api",
    });
  });

  it("uses the adapter for network and missing-endpoint failures without a build-mode gate", async () => {
    vi.mocked(postJSON).mockRejectedValue(new APIError(404, "missing"));

    await expect(requestRegistration(request)).resolves.toMatchObject({
      status: "verification_required",
      transport: "temporary_adapter",
    });
    expect(shouldUseTemporaryRegistrationAdapter(new APIError(0, "offline"))).toBe(true);
    expect(shouldUseTemporaryRegistrationAdapter(new APIError(404, "missing"))).toBe(true);
    expect(shouldUseTemporaryRegistrationAdapter(new APIError(400, "invalid"))).toBe(false);
    expect(shouldUseTemporaryRegistrationAdapter(new APIError(503, "down"))).toBe(false);
  });

  it("uses a fragment token and maps deterministic terminal states", async () => {
    const path = temporaryVerificationPath("request-1");
    expect(path).toMatch(/^\/register\/verify#token=/);
    expect(path).not.toContain("?token=");

    const token = new URLSearchParams(path.split("#")[1]).get("token") ?? "";
    await expect(verifyRegistrationAdapter(token)).resolves.toEqual({ status: "registered" });
    await expect(verifyRegistrationAdapter("smartai-registration-adapter-v1.expired.request-1"))
      .rejects.toMatchObject({ status: 410 });
    await expect(verifyRegistrationAdapter("smartai-registration-adapter-v1.used.request-1"))
      .rejects.toMatchObject({ status: 409 });
  });
});

import { describe, expect, it } from "vitest";
import { APIError } from "./client";
import { developmentVerificationPath, verifyRegistrationMock } from "./registration.mock";
import { shouldUseDevelopmentRegistrationMock } from "./registration";

describe("registration client mock boundary", () => {
  it("falls back only for development network and missing-endpoint failures", () => {
    expect(shouldUseDevelopmentRegistrationMock(new APIError(0, "offline"), true)).toBe(true);
    expect(shouldUseDevelopmentRegistrationMock(new APIError(404, "missing"), true)).toBe(true);
    expect(shouldUseDevelopmentRegistrationMock(new APIError(503, "down"), true)).toBe(false);
    expect(shouldUseDevelopmentRegistrationMock(new APIError(404, "missing"), false)).toBe(false);
  });

  it("uses a fragment token and maps deterministic terminal states", async () => {
    const path = developmentVerificationPath("request-1");
    expect(path).toMatch(/^\/register\/verify#token=/);
    expect(path).not.toContain("?token=");

    const token = new URLSearchParams(path.split("#")[1]).get("token") ?? "";
    await expect(verifyRegistrationMock(token)).resolves.toEqual({ status: "registered" });
    await expect(verifyRegistrationMock("smartai-dev-link-v1.expired.request-1"))
      .rejects.toMatchObject({ status: 410 });
    await expect(verifyRegistrationMock("smartai-dev-link-v1.used.request-1"))
      .rejects.toMatchObject({ status: 409 });
  });
});

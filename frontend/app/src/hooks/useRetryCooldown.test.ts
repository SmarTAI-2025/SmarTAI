import { describe, expect, it } from "vitest";
import { APIError } from "@/api/client";
import { rateLimitDelay } from "@/hooks/useRetryCooldown";

describe("rateLimitDelay", () => {
  it("prioritizes a known stable non-rate-limit code over a conflicting HTTP 429", () => {
    const error = new APIError(
      429,
      "verification_link_invalid",
      { detail: { code: "verification_link_invalid" } },
      90,
    );
    expect(rateLimitDelay(error, "registration_rate_limited")).toBe(0);
  });

  it("uses the stable rate-limit code first and HTTP 429 for missing or unknown codes", () => {
    expect(rateLimitDelay(new APIError(
      400,
      "registration_rate_limited",
      { detail: { code: "registration_rate_limited" } },
      30,
    ), "registration_rate_limited")).toBe(30);
    expect(rateLimitDelay(new APIError(429, "rate limited", undefined, 20), "registration_rate_limited")).toBe(20);
    expect(rateLimitDelay(new APIError(
      429,
      "future_rate_limit_code",
      { detail: { code: "future_rate_limit_code" } },
      10,
    ), "registration_rate_limited")).toBe(10);
  });
});

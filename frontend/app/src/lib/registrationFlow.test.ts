import { beforeEach, describe, expect, it } from "vitest";
import {
  PENDING_REGISTRATION_STORAGE_KEY,
  createPendingRegistrationFlow,
  maskEmail,
  readPendingRegistrationFlow,
  savePendingRegistrationFlow,
} from "@/lib/registrationFlow";

const response = {
  status: "verification_required",
  request_id: "request-1",
  expires_in_seconds: 1800,
  resend_after_seconds: 60,
} as const;

describe("pending registration metadata", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("stores a masked email and never persists the full email or password", () => {
    const flow = createPendingRegistrationFlow("teacher@ustc.edu.cn", response, 1_000_000);
    savePendingRegistrationFlow(flow);
    const raw = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY) ?? "";
    expect(raw).toContain("t*****r@ustc.edu.cn");
    expect(raw).not.toContain("teacher@ustc.edu.cn");
    expect(raw).not.toContain("password");
    expect(raw).not.toContain("token");
  });

  it("rejects expired and implausibly future metadata instead of guessing a request id", () => {
    savePendingRegistrationFlow(createPendingRegistrationFlow("teacher@ustc.edu.cn", response, 1_000_000));
    expect(readPendingRegistrationFlow(1_000_000 + 1_800_001)).toBeNull();

    const future = createPendingRegistrationFlow("teacher@ustc.edu.cn", response, 10_000_000);
    savePendingRegistrationFlow(future);
    expect(readPendingRegistrationFlow(1_000_000)).toBeNull();
  });

  it("clears metadata with a missing opaque request id", () => {
    window.sessionStorage.setItem(PENDING_REGISTRATION_STORAGE_KEY, JSON.stringify({
      ...createPendingRegistrationFlow("teacher@ustc.edu.cn", response, 1_000_000),
      requestId: "",
    }));

    expect(readPendingRegistrationFlow(1_000_001)).toBeNull();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBeNull();
  });

  it("masks short and normal local parts", () => {
    expect(maskEmail("a@ustc.edu.cn")).toBe("*@ustc.edu.cn");
    expect(maskEmail("ab@ustc.edu.cn")).toBe("a*@ustc.edu.cn");
    expect(maskEmail("teacher@ustc.edu.cn")).toBe("t*****r@ustc.edu.cn");
  });
});

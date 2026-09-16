import { beforeEach, describe, expect, it } from "vitest";
import {
  REGISTRATION_FLOW_STORAGE_KEY,
  createPendingRegistrationFlow,
  readPendingRegistrationFlow,
  savePendingRegistrationFlow,
  updatePendingRegistrationFlow,
} from "./registrationFlow";

describe("registration flow session state", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("stores only the short-lived navigation context", () => {
    const flow = createPendingRegistrationFlow({
      username: "teacher",
      email: "teacher@example.edu",
      password: "correct horse battery staple",
    }, {
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    }, "api", 1_000);

    savePendingRegistrationFlow(flow);

    const raw = window.sessionStorage.getItem(REGISTRATION_FLOW_STORAGE_KEY) ?? "";
    expect(raw).not.toContain("correct horse battery staple");
    expect(raw).not.toContain("token");
    expect(readPendingRegistrationFlow(2_000)).toEqual(flow);
  });

  it("updates the request id and absolute deadlines after resend", () => {
    const initial = createPendingRegistrationFlow({
      username: "teacher",
      email: "teacher@example.edu",
      password: "not-stored",
    }, {
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    }, "api", 1_000);

    const updated = updatePendingRegistrationFlow(initial, {
      status: "verification_required",
      request_id: "request-2",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    }, 5_000);

    expect(updated.requestId).toBe("request-2");
    expect(updated.expiresAt).toBe(1_805_000);
    expect(updated.resendAvailableAt).toBe(65_000);
  });

  it("rejects malformed or implausibly long-lived stored data", () => {
    window.sessionStorage.setItem(REGISTRATION_FLOW_STORAGE_KEY, JSON.stringify({ version: 1 }));
    expect(readPendingRegistrationFlow()).toBeNull();

    const now = Date.now();
    window.sessionStorage.setItem(REGISTRATION_FLOW_STORAGE_KEY, JSON.stringify({
      version: 1,
      requestId: "request-1",
      username: "teacher",
      email: "teacher@example.edu",
      createdAt: now,
      expiresAt: now + 3 * 60 * 60 * 1000,
      resendAvailableAt: now,
      transport: "api",
    }));
    expect(readPendingRegistrationFlow(now)).toBeNull();
  });
});

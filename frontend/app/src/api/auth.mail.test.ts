import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "@/api/client";
import {
  confirmPasswordReset,
  requestPasswordReset,
  requestRegistration,
  resendRegistration,
  verifyRegistration,
} from "@/api/auth";

const validRegistration = {
  status: "verification_required",
  request_id: "request-1",
  expires_in_seconds: 1800,
  resend_after_seconds: 60,
} as const;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("public mail auth API contract", () => {
  it("marks every Mail A/B request public so stale auth cannot be attached or refreshed", async () => {
    const post = vi.spyOn(client, "postJSON")
      .mockResolvedValueOnce(validRegistration)
      .mockResolvedValueOnce(validRegistration)
      .mockResolvedValueOnce({ status: "registered" })
      .mockResolvedValueOnce({ status: "reset_link_requested", expires_in_seconds: 1800, resend_after_seconds: 60 })
      .mockResolvedValueOnce({ status: "password_reset" });

    await requestRegistration({ username: "teacher", email: "teacher@ustc.edu.cn", password: "safe-password" });
    await resendRegistration("request-1");
    await verifyRegistration("verification-secret");
    await requestPasswordReset({ email: "teacher@ustc.edu.cn" });
    await confirmPasswordReset("reset-secret", "new-safe-password");

    expect(post).toHaveBeenCalledTimes(5);
    for (const call of post.mock.calls) {
      expect(call[2]).toMatchObject({ _skipAuthHeader: true, _skipAuthRefresh: true });
    }
  });

  it("rejects malformed registration success instead of navigating to a fake success", async () => {
    vi.spyOn(client, "postJSON").mockResolvedValue({
      ...validRegistration,
      request_id: "",
    });
    await expect(requestRegistration({ username: "teacher", email: "teacher@ustc.edu.cn", password: "safe-password" }))
      .rejects.toMatchObject({ status: 502, message: "public_auth_response_invalid" });
  });

  it("rejects non-finite seconds and extra login credentials in Mail responses", async () => {
    const post = vi.spyOn(client, "postJSON")
      .mockResolvedValueOnce({ status: "reset_link_requested", expires_in_seconds: Number.NaN, resend_after_seconds: 60 })
      .mockResolvedValueOnce({ status: "password_reset", token: "must-not-be-accepted" });

    await expect(requestPasswordReset({ email: "teacher@ustc.edu.cn" }))
      .rejects.toMatchObject({ status: 502, message: "public_auth_response_invalid" });
    await expect(confirmPasswordReset("reset-secret", "new-safe-password"))
      .rejects.toMatchObject({ status: 502, message: "public_auth_response_invalid" });
  });

  it("accepts only the frozen verify statuses", async () => {
    const post = vi.spyOn(client, "postJSON")
      .mockResolvedValueOnce({ status: "already_verified" })
      .mockResolvedValueOnce({ status: "success" });
    await expect(verifyRegistration("verification-secret")).resolves.toEqual({ status: "already_verified" });
    await expect(verifyRegistration("verification-secret"))
      .rejects.toMatchObject({ status: 502, message: "public_auth_response_invalid" });
  });
});

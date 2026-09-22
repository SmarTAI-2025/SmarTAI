import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import {
  PENDING_REGISTRATION_STORAGE_KEY,
  createPendingRegistrationFlow,
  savePendingRegistrationFlow,
} from "@/lib/registrationFlow";
import { RegisterCheckEmailPage } from "@/routes/RegisterCheckEmailPage";

vi.mock("@/api/hooks", () => ({ useResendRegistration: vi.fn() }));

const { useResendRegistration } = await import("@/api/hooks");
const mutateAsync = vi.fn();
const resetMutation = vi.fn();

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={["/register/check-email"]}>
        <RegisterCheckEmailPage />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("RegisterCheckEmailPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    vi.clearAllMocks();
    window.sessionStorage.clear();
    (useResendRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync,
      reset: resetMutation,
      isPending: false,
    });
  });

  afterEach(() => vi.useRealTimers());

  it("shows recovery when safe pending metadata is absent", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "请重新填写注册信息" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新发送验证链接" })).not.toBeInTheDocument();
  });

  it("shows only a masked email and replaces the request id after resend", async () => {
    const now = Date.now();
    savePendingRegistrationFlow(createPendingRegistrationFlow("teacher@ustc.edu.cn", {
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 0,
    }, now));
    mutateAsync.mockResolvedValue({
      status: "verification_required",
      request_id: "request-2",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    renderPage();

    expect(screen.getByText(/t\*+r@ustc\.edu\.cn/)).toBeInTheDocument();
    expect(screen.queryByText("teacher@ustc.edu.cn")).not.toBeInTheDocument();
    await act(async () => {
      screen.getByRole("button", { name: "重新发送验证链接" }).click();
    });
    expect(mutateAsync).toHaveBeenCalledWith("request-1");
    expect(resetMutation).toHaveBeenCalledTimes(1);
    const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY) ?? "";
    expect(stored).toContain("request-2");
    expect(stored).not.toContain("request-1");
    expect(stored).not.toContain("teacher@ustc.edu.cn");
    expect(screen.getByRole("button", { name: "01:00 后可重新发送" })).toBeDisabled();
  });

  it("enforces the returned cooldown before allowing resend", async () => {
    mutateAsync.mockResolvedValue({
      status: "verification_required",
      request_id: "request-2",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    savePendingRegistrationFlow(createPendingRegistrationFlow("teacher@ustc.edu.cn", {
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    }, Date.now()));
    renderPage();
    expect(screen.getByRole("button", { name: "01:00 后可重新发送" })).toBeDisabled();
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "重新发送验证链接" })).toBeEnabled();
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });

  it.each(["verification_link_expired", "verification_link_invalid"])(
    "clears stale metadata and offers registration recovery for %s",
    async (code) => {
      savePendingRegistrationFlow(createPendingRegistrationFlow("teacher@ustc.edu.cn", {
        status: "verification_required",
        request_id: "request-1",
        expires_in_seconds: 1800,
        resend_after_seconds: 0,
      }, Date.now()));
      mutateAsync.mockRejectedValue(new APIError(400, code, { detail: { code } }));
      renderPage();

      await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());

      expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBeNull();
      expect(screen.getByRole("heading", { name: "请重新填写注册信息" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "重新填写" })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );
});

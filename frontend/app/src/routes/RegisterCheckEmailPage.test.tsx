import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
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
    <StrictMode><I18nProvider>
      <MemoryRouter initialEntries={["/register/check-email"]}>
        <Routes>
          <Route path="/register/check-email" element={<RegisterCheckEmailPage />} />
          <Route path="/register" element={<h1>注册页</h1>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider></StrictMode>,
  );
}

describe("RegisterCheckEmailPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    vi.clearAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    HTMLDialogElement.prototype.close = function () {
      this.removeAttribute("open");
      queueMicrotask(() => this.dispatchEvent(new Event("close")));
    };
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

  it.each(["verification_link_expired", "verification_link_invalid", "verification_link_already_used"])(
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

  it.each([
    ["zh-CN", "registration_invite_required", "需要填写邀请码", "返回注册页", "重新发送验证链接"],
    ["zh-CN", "registration_invite_invalid", "邀请码已使用或已失效", "返回注册页", "重新发送验证链接"],
    ["en-US", "registration_invite_required", "Invitation code required", "Return to registration", "Resend verification link"],
    ["en-US", "registration_invite_invalid", "Invitation code already used or no longer valid", "Return to registration", "Resend verification link"],
  ])("%s / %s explains the cause and returns directly without a save prompt", async (locale, code, title, action, resend) => {
    window.localStorage.setItem("smartai_locale", locale);
    saveReadyFlow();
    mutateAsync.mockRejectedValue(new APIError(400, code, { detail: { code } }));
    renderPage();
    await act(async () => {
      screen.getByRole("button", { name: resend }).click();
      screen.getByRole("button", { name: resend }).click();
    });
    const dialog = screen.getByRole("alertdialog", { name: title });
    expect(dialog).toHaveAttribute("open");
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByText("请输入邀请码。")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBeNull();
    expect(screen.getByRole("button", { name: resend })).toBeDisabled();
    await act(async () => screen.getByRole("button", { name: resend }).click());
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    await act(async () => within(dialog).getByRole("button", { name: action }).click());
    expect(screen.getByRole("heading", { name: "注册页" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("Escape returns to registration instead of exposing an unusable resend action", async () => {
    saveReadyFlow();
    mutateAsync.mockRejectedValue(new APIError(400, "registration_invite_invalid", { detail: { code: "registration_invite_invalid" } }));
    renderPage();
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    fireEvent(screen.getByRole("alertdialog"), new Event("cancel", { cancelable: true }));
    expect(screen.getByRole("heading", { name: "注册页" })).toBeInTheDocument();
  });

  it("refreshing after invitation rejection cannot restore the rejected request", async () => {
    saveReadyFlow();
    mutateAsync.mockRejectedValue(new APIError(400, "registration_invite_invalid", { detail: { code: "registration_invite_invalid" } }));
    const view = renderPage();
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    view.unmount();
    renderPage();
    expect(screen.getByRole("heading", { name: "请重新填写注册信息" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新发送验证链接" })).not.toBeInTheDocument();
  });

  it.each([0, 503])("keeps the request retryable for transient status %s", async (status) => {
    saveReadyFlow();
    mutateAsync.mockRejectedValue(new APIError(status, "registration_unavailable"));
    renderPage();
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新发送验证链接" })).toBeEnabled();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toContain("request-1");
  });

  it("keeps a rate-limited request and respects Retry-After", async () => {
    saveReadyFlow();
    mutateAsync.mockRejectedValue(new APIError(429, "registration_rate_limited", undefined, 60));
    renderPage();
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "01:00 后可重新发送" })).toBeDisabled();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toContain("request-1");
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "重新发送验证链接" })).toBeEnabled();
  });
});

function saveReadyFlow() {
  savePendingRegistrationFlow(createPendingRegistrationFlow("teacher@ustc.edu.cn", {
    status: "verification_required", request_id: "request-1",
    expires_in_seconds: 1800, resend_after_seconds: 0,
  }, Date.now()));
}

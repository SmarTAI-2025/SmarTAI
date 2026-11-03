import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RegisterVerifyPage } from "@/routes/RegisterVerifyPage";
import { RegisterPage } from "@/routes/RegisterPage";
import { RegisterCheckEmailPage } from "@/routes/RegisterCheckEmailPage";
import { createPendingRegistrationFlow, PENDING_REGISTRATION_STORAGE_KEY, savePendingRegistrationFlow } from "@/lib/registrationFlow";
import type { EmailRegistrationVerifyResponse } from "@/types";

vi.mock("@/api/hooks", () => ({ useVerifyRegistration: vi.fn(), useRequestRegistration: vi.fn(), useResendRegistration: vi.fn() }));
vi.mock("@/api/auth", () => ({ checkRegistrationUsername: vi.fn() }));

const { useVerifyRegistration, useRequestRegistration, useResendRegistration } = await import("@/api/hooks");
const { checkRegistrationUsername } = await import("@/api/auth");
const mutateAsync = vi.fn();
const resetMutation = vi.fn();
const requestRegistration = vi.fn();
const resendRegistration = vi.fn();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.hash}`}</output>;
}

function renderPage(entry = "/register/verify#token=verification-secret") {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/register/verify" element={<><RegisterVerifyPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("RegisterVerifyPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    (useVerifyRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync,
      reset: resetMutation,
      isPending: false,
    });
    vi.mocked(useRequestRegistration).mockReturnValue({ mutateAsync: requestRegistration, reset: vi.fn(), isPending: false } as unknown as ReturnType<typeof useRequestRegistration>);
    vi.mocked(useResendRegistration).mockReturnValue({ mutateAsync: resendRegistration, reset: vi.fn(), isPending: false } as unknown as ReturnType<typeof useResendRegistration>);
    vi.mocked(checkRegistrationUsername).mockResolvedValue(true);
  });

  afterEach(() => vi.useRealTimers());

  it("removes the token fragment immediately and waits for active confirmation", async () => {
    mutateAsync.mockResolvedValue({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    expect(screen.getByTestId("location")).not.toHaveTextContent("verification-secret");
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(mutateAsync).toHaveBeenCalledWith("verification-secret");
    expect(resetMutation).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.getItem("verification-secret")).toBeNull();
  });

  it("treats already_verified as a completed registration", async () => {
    mutateAsync.mockResolvedValue({ status: "already_verified" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(screen.getByText("邮箱已验证")).toBeInTheDocument();
  });

  it.each(["registration_invite_invalid", "registration_invite_required"])("explains invitation failure (%s) and offers a new registration", async (code) => {
    mutateAsync.mockRejectedValue(new APIError(400, code, { detail: { code } }));
    renderPage();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(await screen.findByRole("heading", { name: "邀请码不可用" })).toBeInTheDocument();
    expect(screen.getByText(/账号尚未创建，请使用有效且未使用的邀请码重新注册/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新注册" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "重试验证" })).not.toBeInTheDocument();
  });

  it.each([
    ["verification_link_expired", "重置链接"],
    ["verification_link_already_used", "此链接不可再用"],
    ["verification_link_invalid", "无法验证此链接"],
  ])("maps %s without exposing the raw token", async (code, expected) => {
    mutateAsync.mockRejectedValue(new APIError(400, code, { detail: { code } }));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    if (code === "verification_link_expired") {
      expect(screen.getByRole("heading", { name: "需要新的验证链接" })).toBeInTheDocument();
    } else {
      expect(screen.getByRole("heading", { name: expected })).toBeInTheDocument();
    }
    expect(document.body).not.toHaveTextContent("verification-secret");
  });

  it("retains the in-memory token only for a safe network retry", async () => {
    mutateAsync
      .mockRejectedValueOnce(new APIError(0, "Network error"))
      .mockResolvedValueOnce({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法完成验证");
    await act(async () => screen.getByRole("button", { name: "重试验证" }).click());
    expect(mutateAsync).toHaveBeenNthCalledWith(1, "verification-secret");
    expect(mutateAsync).toHaveBeenNthCalledWith(2, "verification-secret");
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
  });

  it.each([404, 408])("treats HTTP %s as a service retry, not an invalid link", async (status) => {
    mutateAsync
      .mockRejectedValueOnce(new APIError(status, "service unavailable"))
      .mockResolvedValueOnce({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());

    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法完成验证");
    expect(screen.queryByRole("heading", { name: "无法验证此链接" })).not.toBeInTheDocument();
    await act(async () => screen.getByRole("button", { name: "重试验证" }).click());
    expect(mutateAsync).toHaveBeenNthCalledWith(2, "verification-secret");
  });

  it("enforces Retry-After before the verification token can be retried", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    mutateAsync.mockRejectedValue(new APIError(
      429,
      "registration_rate_limited",
      { detail: { code: "registration_rate_limited" } },
      60,
    ));
    renderPage();
    await act(async () => undefined);
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());

    const blocked = screen.getByRole("button", { name: "01:00 后可重试" });
    expect(blocked).toBeDisabled();
    blocked.click();
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "重试验证" })).toBeEnabled();
  });

  it.each(["registered", "already_verified", "registration_invite_invalid", "registration_rate_limited", "network"])(
    "ignores late %s after leaving and preserves a new real-form flow through refresh and resend",
    async (result) => {
      const pending = deferredVerification();
      mutateAsync.mockReturnValue(pending.promise);
      requestRegistration.mockResolvedValue(registrationResponse("new-flow"));
      resendRegistration.mockResolvedValue(registrationResponse("new-flow-resent"));
      saveFlow("old-flow");
      const view = renderJourney();
      await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
      fireEvent.click(screen.getByRole("link", { name: "SmarTAI 首页" }));
      fireEvent.click(screen.getByRole("link", { name: "注册" }));
      fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "new-teacher" } });
      fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: "new-teacher@ustc.edu.cn" } });
      fireEvent.change(screen.getByRole("textbox", { name: "邀请码" }), { target: { value: "new-unused-invite" } });
      for (const input of document.querySelectorAll('input[type="password"]')) {
        fireEvent.change(input, { target: { value: "new-password-123" } });
      }
      await act(async () => screen.getByRole("button", { name: "发送验证链接" }).click());
      expect(requestRegistration).toHaveBeenCalledWith(expect.objectContaining({ invite_code: "new-unused-invite" }));
      const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
      expect(stored).toContain("new-flow");
      await act(async () => settleVerification(pending, result));
      expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
      expect(resetMutation).not.toHaveBeenCalled();
      expect(screen.getByRole("heading", { name: "请查收学校邮箱" })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      view.unmount();
      renderJourney("/register/check-email");
      await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
      expect(resendRegistration).toHaveBeenCalledWith("new-flow");
      expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toContain("new-flow-resent");
      expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).not.toContain("verification-secret");
    },
  );

  it.each(["old-flow", null])("preserves a replacement flow while verification stays mounted (initial %s)", async (initial) => {
    const pending = deferredVerification();
    mutateAsync.mockReturnValue(pending.promise);
    if (initial) saveFlow(initial);
    renderPage();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    saveFlow("new-flow");
    const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    await act(async () => pending.resolve({ status: "registered", request_id: "old-flow" }));
    expect(screen.getByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
  });

  it("does not erase expired pending metadata while handling an unrelated verification", async () => {
    const pending = deferredVerification();
    mutateAsync.mockReturnValue(pending.promise);
    savePendingRegistrationFlow(createPendingRegistrationFlow("pending@ustc.edu.cn", registrationResponse("another-flow"), Date.now() - 1_801_000));
    const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    renderPage();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    await act(async () => pending.resolve({ status: "registered", request_id: "old-flow" }));
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
  });

  it.each(["registered", "already_verified"] as const)("clears only the flow identified by the %s response", async (status) => {
    saveFlow("matching-flow");
    mutateAsync.mockResolvedValue({ status, request_id: "matching-flow" });
    renderPage();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(screen.getByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBeNull();
  });

  it.each([undefined, "another-request"])("preserves an existing different flow when the response identifies %s", async (requestId) => {
    saveFlow("current-flow");
    const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    mutateAsync.mockResolvedValue({ status: "registered", ...(requestId ? { request_id: requestId } : {}) });
    renderPage();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(screen.getByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
  });
});

function registrationResponse(requestId: string) {
  return { status: "verification_required" as const, request_id: requestId, expires_in_seconds: 1800, resend_after_seconds: 0 };
}

function saveFlow(requestId: string) {
  savePendingRegistrationFlow(createPendingRegistrationFlow("pending@ustc.edu.cn", registrationResponse(requestId)));
}

function renderJourney(entry = "/register/verify#token=verification-secret") {
  return render(<StrictMode><I18nProvider><MemoryRouter initialEntries={[entry]}><Routes>
    <Route path="/register/verify" element={<RegisterVerifyPage />} />
    <Route path="/" element={<Link to="/register">注册</Link>} />
    <Route path="/register" element={<RegisterPage />} />
    <Route path="/register/check-email" element={<RegisterCheckEmailPage />} />
  </Routes></MemoryRouter></I18nProvider></StrictMode>);
}

function deferredVerification() {
  let resolve!: (value: EmailRegistrationVerifyResponse) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<EmailRegistrationVerifyResponse>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function settleVerification(pending: ReturnType<typeof deferredVerification>, result: string) {
  if (result === "registered" || result === "already_verified") pending.resolve({ status: result, request_id: "old-flow" });
  else pending.reject(new APIError(result === "network" ? 0 : result === "registration_rate_limited" ? 429 : 400, result, { detail: { code: result } }, result === "registration_rate_limited" ? 60 : undefined));
}

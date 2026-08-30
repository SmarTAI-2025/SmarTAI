import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError, SMARTAI_TOKEN_STORAGE_KEY } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ForgotPasswordPage } from "@/routes/ForgotPasswordPage";
import { PasswordResetCheckEmailPage } from "@/routes/PasswordResetCheckEmailPage";
import { ResetPasswordPage } from "@/routes/ResetPasswordPage";
import {
  PASSWORD_RESET_REQUEST_STORAGE_KEY,
  createPasswordResetRequestMarker,
  savePasswordResetRequestMarker,
} from "@/lib/passwordResetRequestFlow";

vi.mock("@/api/hooks", () => ({
  useRequestPasswordReset: vi.fn(),
  useConfirmPasswordReset: vi.fn(),
}));

const { useRequestPasswordReset, useConfirmPasswordReset } = await import("@/api/hooks");
const requestReset = vi.fn();
const resetRequestMutation = vi.fn();
const confirmReset = vi.fn();
const resetConfirmMutation = vi.fn();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.hash}`}</output>;
}

function renderForgot(queryClient = new QueryClient()) {
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <MemoryRouter initialEntries={["/forgot-password"]}>
          <Routes>
            <Route path="/forgot-password" element={<ForgotPasswordPage />} />
            <Route path="/forgot-password/check-email" element={<><PasswordResetCheckEmailPage /><LocationProbe /></>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

function renderReset(queryClient: QueryClient, entry = "/reset-password#token=reset-secret") {
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/reset-password" element={<><ResetPasswordPage /><LocationProbe /></>} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

function renderResetCheck() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={["/forgot-password/check-email"]}>
        <Routes>
          <Route path="/forgot-password" element={<LocationProbe />} />
          <Route path="/forgot-password/check-email" element={<><PasswordResetCheckEmailPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("password reset pages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    (useRequestPasswordReset as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync: requestReset,
      reset: resetRequestMutation,
      isPending: false,
    });
    (useConfirmPasswordReset as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync: confirmReset,
      reset: resetConfirmMutation,
      isPending: false,
    });
  });

  afterEach(() => vi.useRealTimers());

  it("submits a normalized email, clears mutation variables, and shows the neutral result", async () => {
    requestReset.mockResolvedValue({ status: "reset_link_requested", expires_in_seconds: 1800, resend_after_seconds: 60 });
    const user = userEvent.setup();
    renderForgot();
    await user.type(screen.getByRole("textbox", { name: "学校邮箱" }), "Teacher@USTC.edu.cn");
    await user.click(screen.getByRole("button", { name: "发送重置邮件" }));
    expect(requestReset).toHaveBeenCalledWith({ email: "teacher@ustc.edu.cn" });
    expect(resetRequestMutation).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: "重置请求已受理" })).toBeInTheDocument();
    expect(screen.getByText(/无论账号存在、停用或不存在/)).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/forgot-password/check-email");
    const stored = window.sessionStorage.getItem(PASSWORD_RESET_REQUEST_STORAGE_KEY) ?? "";
    expect(stored).not.toContain("teacher@ustc.edu.cn");
    expect(stored).not.toContain("password");
    expect(stored).not.toContain("token");
  });

  it("redirects a direct or expired check-email deep link to the request page", async () => {
    const first = renderResetCheck();
    expect(await screen.findByTestId("location")).toHaveTextContent("/forgot-password");
    expect(screen.queryByRole("heading", { name: "重置请求已受理" })).not.toBeInTheDocument();
    first.unmount();

    savePasswordResetRequestMarker(createPasswordResetRequestMarker({
      status: "reset_link_requested",
      expires_in_seconds: 60,
      resend_after_seconds: 60,
    }, Date.now() - 60_001));
    renderResetCheck();
    expect(await screen.findByTestId("location")).toHaveTextContent("/forgot-password");
    expect(window.sessionStorage.getItem(PASSWORD_RESET_REQUEST_STORAGE_KEY)).toBeNull();
  });

  it("restores the neutral check-email page on refresh with a valid safe marker", () => {
    savePasswordResetRequestMarker(createPasswordResetRequestMarker({
      status: "reset_link_requested",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    }));
    const first = renderResetCheck();
    expect(screen.getByRole("heading", { name: "重置请求已受理" })).toBeInTheDocument();
    first.unmount();

    renderResetCheck();
    expect(screen.getByRole("heading", { name: "重置请求已受理" })).toBeInTheDocument();
  });

  it("leaves the accepted page when its short-lived marker expires", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    savePasswordResetRequestMarker(createPasswordResetRequestMarker({
      status: "reset_link_requested",
      expires_in_seconds: 2,
      resend_after_seconds: 1,
    }));
    renderResetCheck();
    expect(screen.getByRole("heading", { name: "重置请求已受理" })).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTime(2_001));
    expect(screen.getByTestId("location")).toHaveTextContent("/forgot-password");
  });

  it("does not navigate on a service failure", async () => {
    requestReset.mockRejectedValue(new APIError(503, "password_reset_unavailable", { detail: { code: "password_reset_unavailable" } }));
    const user = userEvent.setup();
    renderForgot();
    await user.type(screen.getByRole("textbox", { name: "学校邮箱" }), "teacher@ustc.edu.cn");
    await user.click(screen.getByRole("button", { name: "发送重置邮件" }));
    expect(screen.getByRole("alert")).toHaveTextContent("密码找回服务暂不可用");
    expect(screen.queryByRole("heading", { name: "重置请求已受理" })).not.toBeInTheDocument();
    expect(resetRequestMutation).toHaveBeenCalledTimes(1);
  });

  it("keeps rate limiting actionable without changing the neutral account copy", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    requestReset.mockRejectedValue(new APIError(
      429,
      "password_reset_rate_limited",
      { detail: { code: "password_reset_rate_limited" } },
      60,
    ));
    renderForgot();
    fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: "teacher@ustc.edu.cn" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "发送重置邮件" })));

    expect(screen.getByRole("alert")).toHaveTextContent("请在 1 分钟后再试");
    expect(screen.queryByRole("heading", { name: "重置请求已受理" })).not.toBeInTheDocument();
    const blocked = screen.getByRole("button", { name: "01:00 后可重试" });
    expect(blocked).toBeDisabled();
    fireEvent.click(blocked);
    expect(requestReset).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "发送重置邮件" })).toBeEnabled();
  });

  it("strips the reset token immediately and does not consume it on page open", async () => {
    const queryClient = new QueryClient();
    renderReset(queryClient);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    expect(screen.getByTestId("location")).not.toHaveTextContent("reset-secret");
    expect(confirmReset).not.toHaveBeenCalled();
    expect(document.body).not.toHaveTextContent("reset-secret");
  });

  it("requires matching passwords before confirming", async () => {
    const user = userEvent.setup();
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "different-password");
    await user.click(screen.getByRole("button", { name: "重置密码" }));
    expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(confirmReset).not.toHaveBeenCalled();
  });

  it("clears access state, protected query cache, passwords, token, and mutation variables after success", async () => {
    confirmReset.mockResolvedValue({ status: "password_reset" });
    window.localStorage.setItem(SMARTAI_TOKEN_STORAGE_KEY, "old-access-token");
    const queryClient = new QueryClient();
    queryClient.setQueryData(["tasks", "detail", "private"], { secret: "cached" });
    const user = userEvent.setup();
    renderReset(queryClient);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    expect(confirmReset).toHaveBeenCalledTimes(1);
    expect(confirmReset).toHaveBeenCalledWith({ token: "reset-secret", newPassword: "new-password-123" });
    expect(resetConfirmMutation).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: "密码已重置" })).toBeInTheDocument();
    expect(window.localStorage.getItem(SMARTAI_TOKEN_STORAGE_KEY)).toBeNull();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(document.body).not.toHaveTextContent("new-password-123");
    expect(document.body).not.toHaveTextContent("reset-secret");
  });

  it("maps an expired link and clears the submitted secrets", async () => {
    confirmReset.mockRejectedValue(new APIError(400, "password_reset_link_expired", { detail: { code: "password_reset_link_expired" } }));
    const user = userEvent.setup();
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "重置密码" }));
    expect(await screen.findByRole("heading", { name: "重置链接已过期" })).toBeInTheDocument();
    expect(resetConfirmMutation).toHaveBeenCalledTimes(1);
    expect(document.body).not.toHaveTextContent("new-password-123");
    expect(document.body).not.toHaveTextContent("reset-secret");
  });

  it.each([
    ["password_reset_link_already_used", "重置链接已使用"],
    ["password_reset_link_invalid", "重置链接无效"],
  ])("maps %s to a new-request action", async (code, heading) => {
    confirmReset.mockRejectedValue(new APIError(400, code, { detail: { code } }));
    const user = userEvent.setup();
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新申请" })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("reset-secret");
  });

  it("retains the token only for an actionable network retry", async () => {
    const failure = new APIError(0, "Network error");
    const expected = "暂时无法连接重置服务";
    confirmReset.mockRejectedValueOnce(failure).mockResolvedValueOnce({ status: "password_reset" });
    const user = userEvent.setup();
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    expect(screen.getByRole("alert")).toHaveTextContent(expected);
    expect(screen.getByLabelText("新密码")).toHaveValue("");
    await user.type(screen.getByLabelText("新密码"), "replacement-password");
    await user.type(screen.getByLabelText("确认新密码"), "replacement-password");
    await user.click(screen.getByRole("button", { name: "重新输入并重试" }));

    expect(confirmReset).toHaveBeenNthCalledWith(1, { token: "reset-secret", newPassword: "new-password-123" });
    expect(confirmReset).toHaveBeenNthCalledWith(2, { token: "reset-secret", newPassword: "replacement-password" });
    expect(await screen.findByRole("heading", { name: "密码已重置" })).toBeInTheDocument();
  });

  it("treats reset-confirm HTTP 404 as retryable service failure", async () => {
    confirmReset
      .mockRejectedValueOnce(new APIError(404, "route unavailable"))
      .mockResolvedValueOnce({ status: "password_reset" });
    const user = userEvent.setup();
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-123");
    await user.click(screen.getByRole("button", { name: "重置密码" }));

    expect(screen.getByRole("alert")).toHaveTextContent("密码重置服务暂不可用");
    expect(screen.queryByRole("heading", { name: "重置链接无效" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("新密码"), "replacement-password");
    await user.type(screen.getByLabelText("确认新密码"), "replacement-password");
    await user.click(screen.getByRole("button", { name: "重新输入并重试" }));
    expect(confirmReset).toHaveBeenNthCalledWith(2, {
      token: "reset-secret",
      newPassword: "replacement-password",
    });
  });

  it("enforces Retry-After before reset confirmation can reuse the token", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    confirmReset.mockRejectedValue(new APIError(
      429,
      "password_reset_rate_limited",
      { detail: { code: "password_reset_rate_limited" } },
      60,
    ));
    renderReset(new QueryClient());
    await act(async () => undefined);
    fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "new-password-123" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "new-password-123" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "重置密码" })));

    const blocked = screen.getByRole("button", { name: "01:00 后可重试" });
    expect(blocked).toBeDisabled();
    fireEvent.click(blocked);
    expect(confirmReset).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "重新输入并重试" })).toBeEnabled();
  });

  it("guards a synchronous double submit", async () => {
    let resolveReset!: (value: unknown) => void;
    confirmReset.mockReturnValue(new Promise((resolve) => { resolveReset = resolve; }));
    renderReset(new QueryClient());
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/reset-password$/));
    const password = screen.getByLabelText("新密码");
    const confirmation = screen.getByLabelText("确认新密码");
    const user = userEvent.setup();
    await user.type(password, "new-password-123");
    await user.type(confirmation, "new-password-123");
    const submit = screen.getByRole("button", { name: "重置密码" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(confirmReset).toHaveBeenCalledTimes(1);
    resolveReset({ status: "password_reset" });
    await waitFor(() => expect(screen.getByRole("heading", { name: "密码已重置" })).toBeInTheDocument());
  });
});

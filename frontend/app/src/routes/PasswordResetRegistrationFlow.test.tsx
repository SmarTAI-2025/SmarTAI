import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router-dom";
import { StrictMode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { SMARTAI_TOKEN_STORAGE_KEY } from "@/api/client";
import { LocalizedDraftLeaveProvider } from "@/hooks/useDraftLeave";
import { I18nProvider } from "@/i18n/I18nProvider";
import { PENDING_REGISTRATION_STORAGE_KEY } from "@/lib/registrationFlow";
import { RegisterPage } from "@/routes/RegisterPage";
import { RegisterCheckEmailPage } from "@/routes/RegisterCheckEmailPage";
import { ResetPasswordPage } from "@/routes/ResetPasswordPage";
import type { PasswordResetConfirmResponse } from "@/types";

vi.mock("@/api/auth", () => ({
  requestRegistration: vi.fn(),
  checkRegistrationUsername: vi.fn(),
  resendRegistration: vi.fn(),
  confirmPasswordReset: vi.fn(),
}));
const api = await import("@/api/auth");

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  vi.mocked(api.checkRegistrationUsername).mockResolvedValue(true);
  vi.mocked(api.requestRegistration).mockResolvedValue({
    status: "verification_required",
    request_id: "new-registration",
    expires_in_seconds: 1800,
    resend_after_seconds: 0,
  });
});

async function completeRegistrationForm() {
  fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "new-teacher" } });
  fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: "new-teacher@ustc.edu.cn" } });
  fireEvent.change(screen.getByRole("textbox", { name: "邀请码" }), { target: { value: "synthetic-new-invite" } });
  fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: "synthetic-password-123" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "synthetic-password-123" } });
  await act(async () => { screen.getByRole("button", { name: "发送验证链接" }).click(); });
  await waitFor(() => expect(screen.getByRole("heading", { name: "请查收学校邮箱" })).toBeInTheDocument());
}

async function submitReset() {
  fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "synthetic-reset-password-123" } });
  fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "synthetic-reset-password-123" } });
  await act(async () => { screen.getByRole("button", { name: "重置密码" }).click(); });
}

it.each(["normal", "late"])(
  "preserves an unrelated registration after %s password-reset success while clearing authentication",
  async (mode) => {
    let finishReset!: (value: PasswordResetConfirmResponse) => void;
    const delayed = new Promise<PasswordResetConfirmResponse>((resolve) => { finishReset = resolve; });
    vi.mocked(api.confirmPasswordReset).mockReturnValue(
      mode === "late" ? delayed : Promise.resolve({ status: "password_reset" }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const router = createMemoryRouter([{
      element: <LocalizedDraftLeaveProvider><Outlet /></LocalizedDraftLeaveProvider>,
      children: [
        { path: "/register", element: <RegisterPage /> },
        { path: "/register/check-email", element: <RegisterCheckEmailPage /> },
        { path: "/reset-password", element: <ResetPasswordPage /> },
        { path: "/", element: <><Link to="/register">开始新注册</Link><Link to="/reset-password#token=synthetic-reset-token">打开重置邮件</Link></> },
      ],
    }], { initialEntries: [mode === "late" ? "/reset-password#token=synthetic-reset-token" : "/register"] });
    render(
      <StrictMode><I18nProvider><QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider></I18nProvider></StrictMode>,
    );
    if (mode === "late") {
      await submitReset();
      expect(api.confirmPasswordReset).toHaveBeenCalledTimes(1);
      await act(async () => { screen.getByRole("link", { name: "SmarTAI 首页" }).click(); });
      const discard = screen.queryByRole("button", { name: "不暂存并离开" });
      if (discard) await act(async () => { discard.click(); });
      await act(async () => { screen.getByRole("link", { name: "开始新注册" }).click(); });
    }
    await completeRegistrationForm();
    const registration = sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    expect(registration).toContain("new-registration");
    localStorage.setItem(SMARTAI_TOKEN_STORAGE_KEY, "synthetic-old-access-token");
    client.setQueryData(["protected", "sentinel"], { synthetic: true });
    if (mode === "normal") {
      await act(async () => { screen.getByRole("link", { name: "SmarTAI 首页" }).click(); });
      await act(async () => { screen.getByRole("link", { name: "打开重置邮件" }).click(); });
      await submitReset();
      expect(await screen.findByRole("heading", { name: "密码已重置" })).toBeInTheDocument();
    } else {
      await act(async () => { finishReset({ status: "password_reset" }); });
      expect(screen.getByRole("heading", { name: "请查收学校邮箱" })).toBeInTheDocument();
    }
    expect(localStorage.getItem(SMARTAI_TOKEN_STORAGE_KEY)).toBeNull();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    expect(sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(registration);
  },
);

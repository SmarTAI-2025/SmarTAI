import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router-dom";
import { StrictMode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { LocalizedDraftLeaveProvider } from "@/hooks/useDraftLeave";
import { RegisterPage } from "@/routes/RegisterPage";
import { RegisterCheckEmailPage } from "@/routes/RegisterCheckEmailPage";
import { PENDING_REGISTRATION_STORAGE_KEY } from "@/lib/registrationFlow";
import type { EmailRegistrationResponse } from "@/types";

vi.mock("@/api/auth", () => ({ requestRegistration: vi.fn(), checkRegistrationUsername: vi.fn(), resendRegistration: vi.fn() }));
const api = await import("@/api/auth");

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  vi.mocked(api.checkRegistrationUsername).mockResolvedValue(true);
});

it.each(["success", "registration_invite_invalid", "registration_email_domain_not_allowed", "registration_rate_limited", "network"])(
  "preserves a newer form-submitted flow and resend after abandoned request returns %s",
  async (result) => {
    const pending = deferredRegistration();
    vi.mocked(api.requestRegistration).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response("new-flow"));
    vi.mocked(api.resendRegistration).mockResolvedValue(response("new-flow-resent"));
    const { router, view, client } = renderJourney();
    fillRegistration("old-teacher");
    await act(async () => screen.getByRole("button", { name: "发送验证链接" }).click());
    expect(screen.getByRole("button", { name: "正在发送…" })).toBeDisabled();
    expect(api.requestRegistration).toHaveBeenCalledTimes(1);
    await leaveRegistration();
    expect(router.state.location.pathname).toBe("/");
    await act(async () => screen.getByRole("link", { name: "开始新注册" }).click());
    fillRegistration("new-teacher");
    await act(async () => screen.getByRole("button", { name: "发送验证链接" }).click());
    await waitFor(() => expect(screen.getByRole("heading", { name: "请查收学校邮箱" })).toBeInTheDocument());
    const stored = sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    expect(stored).toContain("new-flow");
    await act(async () => settle(pending, result));
    expect(sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBe(stored);
    expect(router.state.location.pathname).toBe("/register/check-email");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    view.unmount();
    client.clear();
    const refreshed = renderJourney("/register/check-email");
    await act(async () => screen.getByRole("button", { name: "重新发送验证链接" }).click());
    expect(api.resendRegistration).toHaveBeenCalledWith("new-flow");
    expect(sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toContain("new-flow-resent");
    expect(sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).not.toMatch(/synthetic-invite|synthetic-password/);
    refreshed.view.unmount();
    refreshed.client.clear();
  },
);

it("does not navigate or restore metadata when an abandoned request succeeds without a replacement", async () => {
  const pending = deferredRegistration();
  vi.mocked(api.requestRegistration).mockReturnValue(pending.promise);
  const { router, view, client } = renderJourney();
  fillRegistration("old-teacher");
  await act(async () => screen.getByRole("button", { name: "发送验证链接" }).click());
  await leaveRegistration();
  await act(async () => pending.resolve(response("old-flow")));
  expect(router.state.location.pathname).toBe("/");
  expect(sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY)).toBeNull();
  view.unmount();
  client.clear();
});

function response(requestId: string): EmailRegistrationResponse {
  return { status: "verification_required", request_id: requestId, expires_in_seconds: 1800, resend_after_seconds: 0 };
}

function fillRegistration(name: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: name } });
  fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: `${name}@ustc.edu.cn` } });
  fireEvent.change(screen.getByRole("textbox", { name: "邀请码" }), { target: { value: `${name}-synthetic-invite` } });
  fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: "synthetic-password-123" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "synthetic-password-123" } });
}

async function leaveRegistration() {
  await act(async () => screen.getByRole("link", { name: "SmarTAI 首页" }).click());
  await screen.findByRole("button", { name: "不暂存并离开" });
  await act(async () => screen.getByRole("button", { name: "不暂存并离开" }).click());
}

function renderJourney(entry = "/register") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter([{ element: <LocalizedDraftLeaveProvider><Outlet /></LocalizedDraftLeaveProvider>, children: [
    { path: "/register", element: <RegisterPage /> },
    { path: "/register/check-email", element: <RegisterCheckEmailPage /> },
    { path: "/", element: <Link to="/register">开始新注册</Link> },
  ] }], { initialEntries: [entry] });
  const view = render(<StrictMode><I18nProvider><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></I18nProvider></StrictMode>);
  return { router, view, client };
}

function deferredRegistration() {
  let resolve!: (value: EmailRegistrationResponse) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<EmailRegistrationResponse>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function settle(pending: ReturnType<typeof deferredRegistration>, result: string) {
  if (result === "success") pending.resolve(response("old-flow"));
  else pending.reject(new APIError(result === "network" ? 0 : result === "registration_rate_limited" ? 429 : 400, result, { detail: { code: result } }, result === "registration_rate_limited" ? 60 : undefined));
}

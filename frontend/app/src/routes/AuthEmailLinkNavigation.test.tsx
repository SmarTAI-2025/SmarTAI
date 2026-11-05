import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError, SMARTAI_TOKEN_STORAGE_KEY } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RegisterVerifyPage } from "@/routes/RegisterVerifyPage";
import { ResetPasswordPage } from "@/routes/ResetPasswordPage";
import type { EmailRegistrationVerifyResponse, PasswordResetConfirmResponse } from "@/types";

vi.mock("@/api/auth", () => ({ verifyRegistration: vi.fn(), confirmPasswordReset: vi.fn() }));
const api = await import("@/api/auth");
const flows = [
  { kind: "verify", path: "/register/verify", ready: "完成邮箱验证", submit: "确认并完成注册", pending: "正在确认…", success: "注册完成", invalid: "无法验证此链接", invalidCode: "verification_link_invalid" },
  { kind: "reset", path: "/reset-password", ready: "设置新密码", submit: "重置密码", pending: "正在重置…", success: "密码已重置", invalid: "重置链接无效", invalidCode: "password_reset_link_invalid" },
] as const;
type Flow = typeof flows[number];

beforeEach(() => { vi.resetAllMocks(); localStorage.clear(); sessionStorage.clear(); });

describe.each(flows)("$kind email hash navigation", (flow) => {
  it("opens the new link after a failed old link without a document reload", async () => {
    rejectNext(flow, new APIError(400, flow.invalidCode, { detail: { code: flow.invalidCode } }));
    resolveNext(flow);
    const { router, view, client } = renderFlow(flow);
    await submit(flow);
    expect(await screen.findByRole("heading", { name: flow.invalid })).toBeInTheDocument();
    await act(async () => router.navigate(`${flow.path}#token=new-token`));
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    expect(screen.getByRole("heading", { name: flow.ready })).toBeInTheDocument();
    await submit(flow);
    expect(await screen.findByRole("heading", { name: flow.success })).toBeInTheDocument();
    expect(requestedTokens(flow)).toEqual(["old-token", "new-token"]);
    view.unmount(); client.clear();
  });

  it("allows explicitly reopening the same token after the previous session completed", async () => {
    resolveNext(flow); resolveNext(flow);
    const { router, view, client } = renderFlow(flow);
    await submit(flow);
    await screen.findByRole("heading", { name: flow.success });
    await act(async () => router.navigate(`${flow.path}#token=old-token`));
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    expect(screen.getByRole("heading", { name: flow.ready })).toBeInTheDocument();
    expect(requestedTokens(flow)).toHaveLength(1);
    await submit(flow);
    await screen.findByRole("heading", { name: flow.success });
    expect(requestedTokens(flow)).toEqual(["old-token", "old-token"]);
    view.unmount(); client.clear();
  });

  it("replaces the old token with an invalid new fragment instead of retaining it", async () => {
    const { router, view, client } = renderFlow(flow);
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    await act(async () => router.navigate(`${flow.path}#unrelated=value`));
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    expect(screen.getByRole("heading", { name: flow.invalid })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: flow.submit })).not.toBeInTheDocument();
    expect(requestedTokens(flow)).toEqual([]);
    view.unmount(); client.clear();
  });

  it.each(["success", "invalid", "rate-limited"])("keeps a new request pending when the old request finishes with %s", async (result) => {
    const oldRequest = deferred();
    const newRequest = deferred();
    pendingNext(flow, oldRequest); pendingNext(flow, newRequest);
    const { router, view, client } = renderFlow(flow);
    await submit(flow);
    await act(async () => router.navigate(`${flow.path}#token=new-token`));
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    expect(screen.getByRole("button", { name: flow.submit })).toBeEnabled();
    await submit(flow);
    expect(requestedTokens(flow)).toEqual(["old-token", "new-token"]);
    await waitFor(() => expect(screen.getByRole("button", { name: flow.pending })).toBeDisabled());
    localStorage.setItem(SMARTAI_TOKEN_STORAGE_KEY, "synthetic-stale-access");
    client.setQueryData(["protected-test-cache"], "stale");
    await act(async () => {
      if (result === "success") oldRequest.resolve(success(flow));
      else {
        const code = result === "invalid" ? flow.invalidCode : `${flow.kind === "verify" ? "registration" : "password_reset"}_rate_limited`;
        oldRequest.reject(new APIError(result === "invalid" ? 400 : 429, code, { detail: { code } }, 60));
      }
    });
    expect(screen.getByRole("button", { name: flow.pending })).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    if (flow.kind === "reset" && result === "success") {
      expect(localStorage.getItem(SMARTAI_TOKEN_STORAGE_KEY)).toBeNull();
      expect(client.getQueryData(["protected-test-cache"])).toBeUndefined();
    }
    await act(async () => newRequest.resolve(success(flow)));
    expect(await screen.findByRole("heading", { name: flow.success })).toBeInTheDocument();
    expect([...Object.values(localStorage), ...Object.values(sessionStorage)].join(" ")).not.toMatch(/old-token|new-token/);
    view.unmount(); client.clear();
  });

  it("does not revive a consumed token after its own strip navigation or an empty-hash navigation", async () => {
    rejectNext(flow, new APIError(400, flow.invalidCode, { detail: { code: flow.invalidCode } }));
    const { router, view, client } = renderFlow(flow);
    await waitFor(() => expect(router.state.location.hash).toBe(""));
    expect(requestedTokens(flow)).toEqual([]);
    await submit(flow);
    await screen.findByRole("heading", { name: flow.invalid });
    await act(async () => router.navigate(flow.path));
    expect(screen.getByRole("heading", { name: flow.invalid })).toBeInTheDocument();
    expect(requestedTokens(flow)).toEqual(["old-token"]);
    view.unmount(); client.clear();
  });
});

function renderFlow(flow: Flow) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter([{ path: flow.path, element: flow.kind === "verify" ? <RegisterVerifyPage /> : <ResetPasswordPage /> }], { initialEntries: [`${flow.path}#token=old-token`] });
  const view = render(<StrictMode><I18nProvider><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></I18nProvider></StrictMode>);
  return { router, view, client };
}

async function submit(flow: Flow) {
  if (flow.kind === "reset") {
    fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "synthetic-password-123" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "synthetic-password-123" } });
  }
  await act(async () => screen.getByRole("button", { name: flow.submit }).click());
}

type Result = EmailRegistrationVerifyResponse | PasswordResetConfirmResponse;
function success(flow: Flow): Result {
  return flow.kind === "verify" ? { status: "registered", request_id: "verified-request" } : { status: "password_reset" };
}
function resolveNext(flow: Flow) {
  if (flow.kind === "verify") vi.mocked(api.verifyRegistration).mockResolvedValueOnce({ status: "registered", request_id: "verified-request" });
  else vi.mocked(api.confirmPasswordReset).mockResolvedValueOnce({ status: "password_reset" });
}
function rejectNext(flow: Flow, error: Error) {
  if (flow.kind === "verify") vi.mocked(api.verifyRegistration).mockRejectedValueOnce(error);
  else vi.mocked(api.confirmPasswordReset).mockRejectedValueOnce(error);
}
function pendingNext(flow: Flow, pending: ReturnType<typeof deferred>) {
  if (flow.kind === "verify") vi.mocked(api.verifyRegistration).mockReturnValueOnce(pending.promise as Promise<EmailRegistrationVerifyResponse>);
  else vi.mocked(api.confirmPasswordReset).mockReturnValueOnce(pending.promise as Promise<PasswordResetConfirmResponse>);
}
function requestedTokens(flow: Flow) {
  return flow.kind === "verify" ? vi.mocked(api.verifyRegistration).mock.calls.map(([token]) => token) : vi.mocked(api.confirmPasswordReset).mock.calls.map(([token]) => token);
}
function deferred() {
  let resolve!: (value: Result) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Result>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

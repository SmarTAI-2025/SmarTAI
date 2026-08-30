import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as authApi from "@/api/auth";
import {
  useConfirmPasswordReset,
  useLogin,
  useRequestPasswordReset,
  useRequestRegistration,
  useVerifyRegistration,
} from "@/api/hooks/auth";

afterEach(() => vi.restoreAllMocks());

describe("public auth mutation cache", () => {
  it("never stores full email, passwords, or fragment tokens as mutation variables", async () => {
    const login = vi.spyOn(authApi, "login").mockResolvedValue({
      token: "login-access-token",
      user: {
        id: "teacher-1",
        username: "teacher",
        email: "account-profile@ustc.edu.cn",
        role: "teacher",
        is_active: true,
        created_at: 1,
      },
    });
    const register = vi.spyOn(authApi, "requestRegistration").mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    const verify = vi.spyOn(authApi, "verifyRegistration").mockResolvedValue({ status: "registered" });
    const forgot = vi.spyOn(authApi, "requestPasswordReset").mockResolvedValue({
      status: "reset_link_requested",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    const confirm = vi.spyOn(authApi, "confirmPasswordReset").mockResolvedValue({ status: "password_reset" });
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false, gcTime: Infinity } },
    });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => ({
      login: useLogin(),
      registration: useRequestRegistration(),
      verification: useVerifyRegistration(),
      passwordRequest: useRequestPasswordReset(),
      passwordConfirm: useConfirmPasswordReset(),
    }), { wrapper });

    await act(async () => {
      await result.current.login.mutateAsync({
        username: "teacher",
        password: "login-password",
      });
      result.current.login.reset();
      await result.current.registration.mutateAsync({
        username: "teacher",
        email: "private.teacher@ustc.edu.cn",
        password: "registration-password",
      });
      result.current.registration.reset();
      await result.current.verification.mutateAsync("verification-fragment-token");
      result.current.verification.reset();
      await result.current.passwordRequest.mutateAsync({ email: "private.teacher@ustc.edu.cn" });
      result.current.passwordRequest.reset();
      await result.current.passwordConfirm.mutateAsync({
        token: "password-reset-fragment-token",
        newPassword: "replacement-password",
      });
      result.current.passwordConfirm.reset();
    });

    expect(login).toHaveBeenCalledWith({ username: "teacher", password: "login-password" });
    expect(register).toHaveBeenCalledWith(expect.objectContaining({ password: "registration-password" }));
    expect(verify).toHaveBeenCalledWith("verification-fragment-token");
    expect(forgot).toHaveBeenCalledWith({ email: "private.teacher@ustc.edu.cn" });
    expect(confirm).toHaveBeenCalledWith("password-reset-fragment-token", "replacement-password");
    const mutations = queryClient.getMutationCache().getAll();
    expect(mutations).toHaveLength(5);
    expect(mutations.map((mutation) => mutation.state.variables)).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
    const cachedState = JSON.stringify(mutations.map((mutation) => mutation.state));
    for (const secret of [
      "private.teacher@ustc.edu.cn",
      "login-password",
      "login-access-token",
      "registration-password",
      "verification-fragment-token",
      "password-reset-fragment-token",
      "replacement-password",
    ]) expect(cachedState).not.toContain(secret);
  });
});

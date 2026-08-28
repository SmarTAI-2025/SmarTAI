import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as authApi from "@/api/auth";
import { clearAuthToken } from "@/api/client";
import { authKeys } from "./keys";

export function useCurrentUser() {
  return useQuery({
    queryKey: authKeys.me,
    queryFn: authApi.restoreSession,
    retry: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: authApi.login,
    onSuccess: (data) => {
      queryClient.setQueryData(authKeys.me, data.user);
    },
  });
}

export function useRequestRegistration() {
  return useMutation({ mutationFn: authApi.requestRegistration });
}

export function useResendRegistration() {
  return useMutation({ mutationFn: authApi.resendRegistration });
}

export function useVerifyRegistration() {
  return useMutation({ mutationFn: authApi.verifyRegistration });
}

export function useRequestPasswordReset() {
  return useMutation({ mutationFn: authApi.requestPasswordReset });
}

export function useConfirmPasswordReset() {
  return useMutation({
    mutationFn: ({ token, newPassword }: { token: string; newPassword: string }) =>
      authApi.confirmPasswordReset(token, newPassword),
  });
}

export function useRefreshToken() {
  return useMutation({
    mutationFn: authApi.refreshToken,
  });
}

export function useLogout() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: authApi.logout,
    onSettled: () => {
      clearAuthToken();
      queryClient.clear();
    },
  });
}

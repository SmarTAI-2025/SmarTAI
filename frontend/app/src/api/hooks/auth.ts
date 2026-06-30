import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as authApi from "@/api/auth";
import { clearAuthToken, getAuthToken } from "@/api/client";
import { authKeys } from "./keys";

export function useCurrentUser() {
  return useQuery({
    queryKey: authKeys.me,
    queryFn: authApi.getCurrentUser,
    enabled: Boolean(getAuthToken()),
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

export function useRegister() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: authApi.register,
    onSuccess: (data) => {
      queryClient.setQueryData(authKeys.me, data.user);
    },
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
      queryClient.removeQueries({ queryKey: authKeys.me });
    },
  });
}

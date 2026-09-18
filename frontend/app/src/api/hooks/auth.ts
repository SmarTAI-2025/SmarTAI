import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
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

  return useEphemeralInputMutation(async (request: Parameters<typeof authApi.login>[0]) => {
    const response = await authApi.login(request);
    return response.user;
  }, {
    onSuccess: (data) => {
      queryClient.setQueryData(authKeys.me, data);
    },
  });
}

export function useRequestRegistration() {
  return useEphemeralInputMutation(authApi.requestRegistration);
}

export function useResendRegistration() {
  return useEphemeralInputMutation(authApi.resendRegistration);
}

export function useVerifyRegistration() {
  return useEphemeralInputMutation(authApi.verifyRegistration);
}

export function useRequestPasswordReset() {
  return useEphemeralInputMutation(authApi.requestPasswordReset);
}

export function useConfirmPasswordReset() {
  return useEphemeralInputMutation(
    ({ token, newPassword }: { token: string; newPassword: string }) =>
      authApi.confirmPasswordReset(token, newPassword),
  );
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

/**
 * React Query retains mutation variables in MutationCache after reset(). Public
 * auth inputs include passwords, full email addresses, and one-time tokens, so
 * the cached mutation is deliberately variable-less. The input lives only
 * until the direct API promise settles and cannot be overwritten concurrently.
 */
function useEphemeralInputMutation<TInput, TOutput>(
  mutationFn: (input: TInput) => Promise<TOutput>,
  options: { onSuccess?: (data: TOutput) => void } = {},
) {
  const inputRef = useRef<TInput | null>(null);
  const activeRef = useRef(false);
  const mutation = useMutation<TOutput, Error, void>({
    mutationFn: () => {
      const input = inputRef.current;
      inputRef.current = null;
      if (input === null) throw new Error("Public auth input is unavailable");
      return mutationFn(input);
    },
    retry: false,
    onSuccess: options.onSuccess,
  });

  const mutateAsync = useCallback(async (input: TInput): Promise<TOutput> => {
    if (activeRef.current) throw new Error("Public auth request is already running");
    activeRef.current = true;
    inputRef.current = input;
    try {
      return await mutation.mutateAsync();
    } finally {
      inputRef.current = null;
      activeRef.current = false;
    }
  }, [mutation.mutateAsync]);

  const reset = useCallback(() => {
    inputRef.current = null;
    activeRef.current = false;
    mutation.reset();
  }, [mutation.reset]);

  return { ...mutation, mutateAsync, reset, variables: undefined };
}

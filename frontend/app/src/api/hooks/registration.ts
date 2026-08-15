import { useMutation } from "@tanstack/react-query";
import * as registrationApi from "@/api/registration";

export function useRequestRegistration() {
  return useMutation({ mutationFn: registrationApi.requestRegistration });
}

export function useResendRegistration() {
  return useMutation({ mutationFn: registrationApi.resendRegistration });
}

export function useVerifyRegistration() {
  return useMutation({ mutationFn: registrationApi.verifyRegistration });
}

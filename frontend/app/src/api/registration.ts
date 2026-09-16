import { normalizeAPIError, postJSON } from "@/api/client";
import type {
  RegistrationRequest,
  RegistrationRequestResult,
  RegistrationRequestResponse,
  RegistrationResendRequest,
  RegistrationResendResponse,
  RegistrationVerificationRequest,
  RegistrationVerificationResponse,
} from "@/types/registration";
import type { Locale } from "@/i18n/messages";

export interface TemporaryRegistrationPreview {
  path: string;
  actionLabel: string;
}

export async function requestRegistration(request: RegistrationRequest): Promise<RegistrationRequestResult> {
  try {
    const response = await postJSON<RegistrationRequestResponse, RegistrationRequest>("/auth/register/request", request);
    return { ...response, transport: "api" };
  } catch (error) {
    if (shouldUseTemporaryRegistrationAdapter(error)) {
      const adapter = await import("./registration.adapter");
      const response = await adapter.requestRegistrationAdapter(request);
      return { ...response, transport: "temporary_adapter" };
    }
    throw error;
  }
}

export async function resendRegistration(request: RegistrationResendRequest): Promise<RegistrationResendResponse> {
  try {
    return await postJSON<RegistrationResendResponse, RegistrationResendRequest>("/auth/register/resend", request);
  } catch (error) {
    if (shouldUseTemporaryRegistrationAdapter(error)) {
      const adapter = await import("./registration.adapter");
      return adapter.resendRegistrationAdapter();
    }
    throw error;
  }
}

export async function verifyRegistration(
  request: RegistrationVerificationRequest,
): Promise<RegistrationVerificationResponse> {
  try {
    return await postJSON<RegistrationVerificationResponse, RegistrationVerificationRequest>(
      "/auth/register/verify",
      request,
    );
  } catch (error) {
    if (shouldUseTemporaryRegistrationAdapter(error)) {
      const adapter = await import("./registration.adapter");
      return adapter.verifyRegistrationAdapter(request.token);
    }
    throw error;
  }
}

export async function getTemporaryVerificationPreview(
  requestId: string,
  locale: Locale,
): Promise<TemporaryRegistrationPreview> {
  const adapter = await import("./registration.adapter");
  return adapter.temporaryVerificationPreview(requestId, locale);
}

export function shouldUseTemporaryRegistrationAdapter(error: unknown): boolean {
  const status = normalizeAPIError(error).status;
  return status === 0 || status === 404;
}

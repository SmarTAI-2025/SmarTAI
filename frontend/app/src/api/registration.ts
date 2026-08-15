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

const IS_DEVELOPMENT = (import.meta.env as ImportMetaEnv & { readonly DEV?: boolean }).DEV === true;

export interface DevelopmentRegistrationPreview {
  path: string;
  title: string;
  description: string;
  actionLabel: string;
}

export async function requestRegistration(request: RegistrationRequest): Promise<RegistrationRequestResult> {
  try {
    const response = await postJSON<RegistrationRequestResponse, RegistrationRequest>("/auth/register/request", request);
    return { ...response, transport: "api" };
  } catch (error) {
    if (IS_DEVELOPMENT && shouldUseDevelopmentRegistrationMock(error, true)) {
      const mock = await import("./registration.mock");
      const response = await mock.requestRegistrationMock(request);
      return { ...response, transport: "development_mock" };
    }
    throw error;
  }
}

export async function resendRegistration(request: RegistrationResendRequest): Promise<RegistrationResendResponse> {
  try {
    return await postJSON<RegistrationResendResponse, RegistrationResendRequest>("/auth/register/resend", request);
  } catch (error) {
    if (IS_DEVELOPMENT && shouldUseDevelopmentRegistrationMock(error, true)) {
      const mock = await import("./registration.mock");
      return mock.resendRegistrationMock();
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
    if (IS_DEVELOPMENT && shouldUseDevelopmentRegistrationMock(error, true)) {
      const mock = await import("./registration.mock");
      return mock.verifyRegistrationMock(request.token);
    }
    throw error;
  }
}

export async function getDevelopmentVerificationPreview(
  requestId: string,
  locale: Locale,
): Promise<DevelopmentRegistrationPreview | null> {
  if (IS_DEVELOPMENT) {
    const mock = await import("./registration.mock");
    return mock.developmentVerificationPreview(requestId, locale);
  }
  return null;
}

export function shouldUseDevelopmentRegistrationMock(error: unknown, isDevelopment: boolean): boolean {
  if (!isDevelopment) return false;
  const status = normalizeAPIError(error).status;
  return status === 0 || status === 404;
}

import { APIError } from "@/api/client";
import type {
  RegistrationRequest,
  RegistrationRequestResponse,
  RegistrationResendResponse,
  RegistrationVerificationResponse,
} from "@/types/registration";
import type { Locale } from "@/i18n/messages";

// Temporary adapter for the current pre-launch development stage.
// The real registration API is always attempted before this module is loaded.
const LINK_TTL_SECONDS = 30 * 60;
const RESEND_COOLDOWN_SECONDS = 60;
const TOKEN_PREFIX = "smartai-registration-adapter-v1";

export async function requestRegistrationAdapter(
  _request: RegistrationRequest,
): Promise<RegistrationRequestResponse> {
  await adapterDelay();
  return response(createRequestId());
}

export async function resendRegistrationAdapter(): Promise<RegistrationResendResponse> {
  await adapterDelay();
  return response(createRequestId());
}

export async function verifyRegistrationAdapter(token: string): Promise<RegistrationVerificationResponse> {
  await adapterDelay();
  const [prefix, outcome, requestId] = token.split(".");
  if (prefix !== TOKEN_PREFIX || !requestId) throw verificationError(400, "verification_link_invalid");
  if (outcome === "expired") throw verificationError(410, "verification_link_expired");
  if (outcome === "used") throw verificationError(409, "verification_link_already_used");
  if (outcome !== "success") throw verificationError(400, "verification_link_invalid");
  return { status: "registered" };
}

export function temporaryVerificationPath(requestId: string, outcome = "success"): string {
  const token = `${TOKEN_PREFIX}.${outcome}.${requestId}`;
  return `/register/verify#token=${encodeURIComponent(token)}`;
}

export function temporaryVerificationPreview(requestId: string, locale: Locale) {
  return {
    path: temporaryVerificationPath(requestId),
    actionLabel: locale === "zh-CN" ? "打开验证链接" : "Open verification link",
  };
}

function response(requestId: string): RegistrationRequestResponse {
  return {
    status: "verification_required",
    request_id: requestId,
    expires_in_seconds: LINK_TTL_SECONDS,
    resend_after_seconds: RESEND_COOLDOWN_SECONDS,
  };
}

function createRequestId(): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `adapter-${random}`;
}

function verificationError(status: number, code: string): APIError {
  return new APIError(status, code, { detail: { code } });
}

async function adapterDelay(): Promise<void> {
  const mode = (import.meta.env as ImportMetaEnv & { readonly MODE?: string }).MODE;
  if (mode === "test") return;
  await new Promise((resolve) => window.setTimeout(resolve, 320));
}

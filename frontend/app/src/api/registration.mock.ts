import { APIError } from "@/api/client";
import type {
  RegistrationRequest,
  RegistrationRequestResponse,
  RegistrationResendResponse,
  RegistrationVerificationResponse,
} from "@/types/registration";
import type { Locale } from "@/i18n/messages";

const LINK_TTL_SECONDS = 30 * 60;
const RESEND_COOLDOWN_SECONDS = 60;
const TOKEN_PREFIX = "smartai-dev-link-v1";

export async function requestRegistrationMock(
  _request: RegistrationRequest,
): Promise<RegistrationRequestResponse> {
  await mockDelay();
  return response(createRequestId());
}

export async function resendRegistrationMock(): Promise<RegistrationResendResponse> {
  await mockDelay();
  return response(createRequestId());
}

export async function verifyRegistrationMock(token: string): Promise<RegistrationVerificationResponse> {
  await mockDelay();
  const [prefix, outcome, requestId] = token.split(".");
  if (prefix !== TOKEN_PREFIX || !requestId) throw verificationError(400, "verification_link_invalid");
  if (outcome === "expired") throw verificationError(410, "verification_link_expired");
  if (outcome === "used") throw verificationError(409, "verification_link_already_used");
  if (outcome !== "success") throw verificationError(400, "verification_link_invalid");
  return { status: "registered" };
}

export function developmentVerificationPath(requestId: string, outcome = "success"): string {
  const token = `${TOKEN_PREFIX}.${outcome}.${requestId}`;
  return `/register/verify#token=${encodeURIComponent(token)}`;
}

export function developmentVerificationPreview(requestId: string, locale: Locale) {
  const zh = locale === "zh-CN";
  return {
    path: developmentVerificationPath(requestId),
    title: zh ? "本地演示" : "Local demonstration",
    description: zh
      ? "当前使用开发环境模拟邮件，不会创建真实账号。"
      : "Email is simulated in development and no real account will be created.",
    actionLabel: zh ? "打开演示验证链接" : "Open demonstration link",
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
  return `dev-${random}`;
}

function verificationError(status: number, code: string): APIError {
  return new APIError(status, code, { detail: { code } });
}

async function mockDelay(): Promise<void> {
  const mode = (import.meta.env as ImportMetaEnv & { readonly MODE?: string }).MODE;
  if (mode === "test") return;
  await new Promise((resolve) => window.setTimeout(resolve, 320));
}

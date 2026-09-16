import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { registrationText } from "@/lib/registrationCopy";
import type { Locale } from "@/i18n/messages";

export type VerificationLinkFailure = "expired" | "used" | "invalid" | "network";

export function localizedRegistrationRequestError(error: unknown, locale: Locale): string {
  const normalized = normalizeAPIError(error);
  if (normalized.status === 0) return registrationText(locale, "requestNetworkError");
  if (normalized.status === 422) return registrationText(locale, "requestInvalid");
  if (normalized.status === 429) {
    return registrationText(locale, "requestRateLimited", {
      time: formatRetryTime(normalized.retryAfterSeconds ?? 60, locale),
    });
  }
  if (normalized.status === 503) return registrationText(locale, "requestUnavailable");
  return registrationText(locale, "requestGenericError");
}

export function verificationLinkFailure(error: unknown): VerificationLinkFailure {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  if (normalized.status === 0 || normalized.status === 429 || normalized.status >= 500) return "network";
  if (code === "verification_link_expired" || normalized.status === 410) return "expired";
  if (code === "verification_link_already_used" || code === "verification_already_completed") return "used";
  return "invalid";
}

function formatRetryTime(seconds: number, locale: Locale): string {
  if (seconds < 60) return locale === "zh-CN" ? `${seconds} 秒` : `${seconds} seconds`;
  const minutes = Math.ceil(seconds / 60);
  return locale === "zh-CN" ? `${minutes} 分钟` : `${minutes} minutes`;
}

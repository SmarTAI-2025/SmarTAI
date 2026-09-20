import { getAPIErrorCode, normalizeAPIError } from "@/api/client";

type Locale = "zh-CN" | "en-US";

export type LinkFailure = "expired" | "used" | "invalid" | "retry";

export function localizedAuthError(
  error: unknown,
  locale: Locale,
  context: "login" | "register",
): string {
  const normalized = normalizeAPIError(error);
  const zh = locale === "zh-CN";

  if (context === "register") return localizedRegistrationRequestError(error, locale);
  if (normalized.status === 0) {
    return zh
      ? "暂时无法连接服务，请检查网络或稍后重试。"
      : "Unable to reach the service. Check your connection and try again.";
  }
  if (normalized.status === 401) {
    return zh ? "用户名或密码不正确。" : "The username or password is incorrect.";
  }
  if (normalized.status === 403) {
    return zh ? "此账号暂时无法访问教师工作台。" : "This account cannot access the teacher workspace.";
  }
  if (normalized.status === 422) {
    return zh ? "请检查用户名和密码后再试。" : "Check the username and password, then try again.";
  }
  return zh ? "登录失败，请稍后重试。" : "Unable to sign in. Try again shortly.";
}

export function localizedRegistrationRequestError(error: unknown, locale: Locale): string {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  const zh = locale === "zh-CN";

  if (code === "registration_email_domain_not_allowed") {
    return zh
      ? "请改用允许的学校邮箱；具体范围以服务端校验为准。"
      : "Use an allowed school email. The server is authoritative for eligible domains.";
  }
  if (code === "registration_rate_limited") {
    return retryMessage(normalized.retryAfterSeconds, locale, "注册邮件请求过于频繁", "Too many registration email requests");
  }
  if (code === "registration_email_delivery_failed") {
    return zh
      ? "邮件暂未发出，请稍后安全重试；账号尚未创建。"
      : "The email was not sent. Retry shortly; no account has been created.";
  }
  if (code === "registration_unavailable" || code === "public_auth_response_invalid") {
    return zh
      ? "注册服务暂不可用或响应异常，请稍后重试。"
      : "Registration is unavailable or returned an invalid response. Try again shortly.";
  }
  if (normalized.status === 0) {
    return zh
      ? "暂时无法连接注册服务，请检查网络后重试。"
      : "Unable to reach registration. Check your connection and try again.";
  }
  if (normalized.status === 422) {
    return zh
      ? "请检查用户名、学校邮箱和密码后再试。"
      : "Check the username, school email, and password, then try again.";
  }
  if (normalized.status === 429) {
    return retryMessage(normalized.retryAfterSeconds, locale, "注册邮件请求过于频繁", "Too many registration email requests");
  }
  if (normalized.status === 401 || normalized.status === 403) {
    return zh
      ? "注册请求被服务拒绝，请稍后重试或联系管理员。"
      : "The service rejected registration. Try again or contact an administrator.";
  }
  if (normalized.status === 404 || normalized.status >= 500) {
    return zh ? "注册服务暂不可用，请稍后重试。" : "Registration is unavailable. Try again shortly.";
  }
  return zh ? "验证邮件未发送，请稍后重试。" : "The verification email was not sent. Try again shortly.";
}

export function registrationLinkFailure(error: unknown): LinkFailure {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  if (code === "verification_link_expired") return "expired";
  if (code === "verification_link_already_used") return "used";
  if (code === "verification_link_invalid") return "invalid";
  if (
    code === "registration_rate_limited"
    || code === "registration_unavailable"
    || code === "public_auth_response_invalid"
  ) return "retry";
  if (normalized.status === 410) return "expired";
  if (
    normalized.status === 0
    || normalized.status === 401
    || normalized.status === 403
    || normalized.status === 404
    || normalized.status === 408
    || normalized.status === 429
    || normalized.status >= 500
  ) {
    return "retry";
  }
  return "invalid";
}

export function localizedRegistrationVerifyError(error: unknown, locale: Locale): string {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  const zh = locale === "zh-CN";
  if (code === "registration_rate_limited" || normalized.status === 429) {
    return retryMessage(normalized.retryAfterSeconds, locale, "验证请求过于频繁", "Too many verification requests");
  }
  if (code === "public_auth_response_invalid") {
    return zh ? "验证服务返回异常响应，账号状态未被前端判定为成功。" : "Verification returned an invalid response, so the client did not mark it successful.";
  }
  if (normalized.status === 401 || normalized.status === 403) {
    return zh ? "验证请求被服务拒绝，请稍后重试。" : "The service rejected verification. Try again shortly.";
  }
  return zh
    ? "暂时无法完成验证，请保持页面打开并重试。"
    : "Unable to complete verification. Keep this page open and try again.";
}

export function localizedPasswordResetRequestError(error: unknown, locale: Locale): string {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  const zh = locale === "zh-CN";
  if (code === "password_reset_rate_limited" || normalized.status === 429) {
    return retryMessage(normalized.retryAfterSeconds, locale, "请求过于频繁", "Too many requests");
  }
  if (code === "password_reset_unavailable" || code === "public_auth_response_invalid") {
    return zh ? "密码找回服务暂不可用，请稍后重试。" : "Password recovery is unavailable. Try again shortly.";
  }
  if (normalized.status === 0) {
    return zh ? "暂时无法连接密码找回服务，请检查网络后重试。" : "Unable to reach password recovery. Check your connection and retry.";
  }
  if (normalized.status === 401 || normalized.status === 403) {
    return zh ? "密码找回请求被服务拒绝，请稍后重试。" : "The service rejected password recovery. Try again shortly.";
  }
  if (normalized.status === 404 || normalized.status >= 500) {
    return zh ? "密码找回服务暂不可用，请稍后重试。" : "Password recovery is unavailable. Try again shortly.";
  }
  return zh ? "暂时无法发送重置邮件，请稍后重试。" : "Unable to request a reset email. Try again shortly.";
}

export function passwordResetLinkFailure(error: unknown): LinkFailure {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  if (code === "password_reset_link_expired") return "expired";
  if (code === "password_reset_link_already_used") return "used";
  if (code === "password_reset_link_invalid") return "invalid";
  if (
    code === "password_reset_rate_limited"
    || code === "password_reset_unavailable"
    || code === "public_auth_response_invalid"
  ) return "retry";
  if (normalized.status === 410) return "expired";
  if (
    normalized.status === 0
    || normalized.status === 401
    || normalized.status === 403
    || normalized.status === 404
    || normalized.status === 408
    || normalized.status === 429
    || normalized.status >= 500
  ) {
    return "retry";
  }
  return "invalid";
}

export function localizedPasswordResetError(error: unknown, locale: Locale): string {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  const zh = locale === "zh-CN";
  if (code === "password_reset_link_expired") {
    return zh ? "重置链接已过期，请重新申请。" : "This reset link has expired. Request a new one.";
  }
  if (code === "password_reset_link_already_used") {
    return zh ? "重置链接已使用，请重新申请或直接登录。" : "This reset link was already used. Request a new one or sign in.";
  }
  if (code === "password_reset_link_invalid") {
    return zh ? "重置链接无效，请重新申请。" : "This reset link is invalid. Request a new one.";
  }
  if (code === "password_reset_rate_limited" || normalized.status === 429) {
    return retryMessage(normalized.retryAfterSeconds, locale, "重置请求过于频繁", "Too many reset requests");
  }
  if (code === "public_auth_response_invalid") {
    return zh ? "重置服务返回异常响应，本地登录状态未按成功处理。" : "Reset returned an invalid response, so the client did not treat it as successful.";
  }
  if (normalized.status === 401 || normalized.status === 403) {
    return zh ? "重置请求被服务拒绝，请重新申请链接。" : "The service rejected this reset. Request a new link.";
  }
  if (normalized.status === 0) {
    return zh ? "暂时无法连接重置服务，请重新输入密码后重试。" : "Unable to reach reset. Re-enter the password and try again.";
  }
  if (normalized.status === 404 || normalized.status === 408 || normalized.status >= 500) {
    return zh ? "密码重置服务暂不可用，请重新输入密码后重试。" : "Password reset is temporarily unavailable. Re-enter the password and try again.";
  }
  return zh ? "密码重置失败，请稍后重试。" : "Unable to reset the password. Try again shortly.";
}

function retryMessage(
  seconds: number | undefined,
  locale: Locale,
  zhPrefix: string,
  enPrefix: string,
): string {
  if (seconds === undefined) return locale === "zh-CN" ? `${zhPrefix}，请稍后再试。` : `${enPrefix}. Try again later.`;
  if (locale === "zh-CN") return `${zhPrefix}，请在 ${formatDuration(seconds, locale)}后再试。`;
  return `${enPrefix}. Try again in ${formatDuration(seconds, locale)}.`;
}

function formatDuration(seconds: number, locale: Locale): string {
  if (seconds < 60) return locale === "zh-CN" ? `${seconds} 秒` : `${seconds} seconds`;
  const minutes = Math.ceil(seconds / 60);
  return locale === "zh-CN" ? `${minutes} 分钟` : `${minutes} minutes`;
}

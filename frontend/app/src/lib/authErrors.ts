import { normalizeAPIError } from "@/api/client";

export function localizedAuthError(
  error: unknown,
  locale: "zh-CN" | "en-US",
  context: "login" | "register",
) {
  const normalized = normalizeAPIError(error);
  const text = normalized.message;
  const zh = locale === "zh-CN";

  if (normalized.status === 0) {
    return zh
      ? "暂时无法连接服务，请检查网络或稍后重试。"
      : "Unable to reach the service. Check your connection and try again.";
  }
  if (normalized.status === 401) {
    return zh ? "用户名或密码不正确。" : "The username or password is incorrect.";
  }
  if (normalized.status === 403) {
    return zh
      ? "公开注册尚未开放，请使用有效邀请码。"
      : "Public registration is closed. Use a valid invitation code.";
  }
  if (normalized.status === 409 || text.includes("already exists")) {
    return zh ? "这个用户名已被使用，请更换后重试。" : "That username is already in use.";
  }
  if (text.includes("not valid for this email")) {
    return zh
      ? "邀请码与填写的邮箱不匹配，请使用受邀邮箱。"
      : "This invitation does not match the email you entered.";
  }
  if (text.includes("Invalid or expired invite code") || text.includes("Invalid invite code")) {
    return zh
      ? "邀请码无效或已过期，请向邀请人获取新邀请码。"
      : "The invitation code is invalid or expired. Ask the inviter for a new one.";
  }
  if (normalized.status === 422) {
    return zh
      ? "请检查用户名、密码和邮箱格式后再试。"
      : "Check the username, password, and email format, then try again.";
  }
  if (context === "login") {
    return zh ? "登录失败，请稍后重试。" : "Unable to sign in. Try again shortly.";
  }
  return zh ? "注册失败，请稍后重试。" : "Unable to create the account. Try again shortly.";
}

export function localizedPasswordResetError(
  error: unknown,
  locale: "zh-CN" | "en-US",
) {
  const normalized = normalizeAPIError(error);
  const zh = locale === "zh-CN";
  const code = normalized.payload?.detail && typeof normalized.payload.detail === "object"
    ? String((normalized.payload.detail as { code?: unknown }).code ?? "")
    : normalized.message;
  if (code === "password_reset_link_expired") {
    return zh ? "重置链接已过期，请重新申请。" : "This reset link has expired. Request a new one.";
  }
  if (code === "password_reset_link_already_used") {
    return zh ? "重置链接已使用，请重新申请。" : "This reset link has already been used. Request a new one.";
  }
  if (code === "password_reset_link_invalid") {
    return zh ? "重置链接无效，请重新申请。" : "This reset link is invalid. Request a new one.";
  }
  if (code === "password_reset_rate_limited") {
    return zh ? "请求过于频繁，请稍后再试。" : "Too many requests. Try again later.";
  }
  return zh ? "密码重置失败，请稍后重试。" : "Unable to reset the password. Try again shortly.";
}

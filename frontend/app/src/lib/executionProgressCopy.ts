import type { Locale } from "@/i18n/messages";

export function executionProgressCopy(message: string, locale: Locale): string | undefined {
  if (message === "provider_rate_limited_wait") return locale === "zh-CN" ? "服务商限流，等待重试" : "Provider rate limit; waiting to retry";
  if (message === "provider_retry_resumed") return locale === "zh-CN" ? "限流等待结束，继续处理" : "Rate-limit wait ended; processing continues";
  return undefined;
}

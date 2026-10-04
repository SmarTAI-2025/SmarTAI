import type { Locale } from "@/i18n/messages";

export function GradingRetryNotice({ locale }: { locale: Locale }) {
  return <p className="text-sm leading-6 text-muted-foreground">
    {locale === "zh-CN"
      ? "将按当前配置重新批改整批并消耗模型额度。旧结果保留，新结果需重新复核。"
      : "Retry the entire batch with the current settings, using model quota. Previous results are kept; review the new results again."}
  </p>;
}

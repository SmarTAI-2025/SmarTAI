import type { Locale } from "@/i18n/messages";

export function GradingRetryNotice({ locale }: { locale: Locale }) {
  return <p className="text-sm leading-6 text-muted-foreground">
    {locale === "zh-CN"
      ? "重试会按当前题目、作答和已保存的模型设置重新批改整批，可能消耗模型额度。旧结果和人工修订会保留；新结果需要重新复核，不会自动套用旧修订。"
      : "Retry grades the entire batch using the current questions, answers and saved model settings, and may use model quota. Previous results and teacher edits are kept. Review the new results again; previous edits are not applied automatically."}
  </p>;
}

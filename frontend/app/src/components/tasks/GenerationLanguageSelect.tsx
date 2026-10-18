import type { ContentLanguage } from "@/api/contentLanguage";
import { useI18n } from "@/i18n/I18nProvider";

export function GenerationLanguageSelect({ value, onChange, disabled = false }: {
  value: ContentLanguage; onChange: (value: ContentLanguage) => void; disabled?: boolean;
}) {
  const { locale } = useI18n();
  return <label className="flex shrink-0 items-center gap-2 text-sm font-medium">
    {locale === "zh-CN" ? "生成语言" : "Generation language"}
    <select className="rounded-lg border bg-background px-3 py-2" value={value} disabled={disabled}
      onChange={event => onChange(event.target.value as ContentLanguage)}>
      <option value="zh">{locale === "zh-CN" ? "中文" : "Chinese"}</option>
      <option value="en">{locale === "zh-CN" ? "英文" : "English"}</option>
    </select>
  </label>;
}

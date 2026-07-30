import { Languages } from "lucide-react";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";

export function LanguageToggle({ className }: { className?: string }) {
  const { locale, setLocale } = useI18n();
  const isChinese = locale === "zh-CN";

  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg border bg-card px-2.5 text-xs font-semibold text-muted-foreground outline-none transition-colors hover:border-primary/30 hover:bg-muted hover:text-primary focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
      onClick={() => setLocale(isChinese ? "en-US" : "zh-CN")}
      aria-label={isChinese ? "切换至英文" : "Switch to Chinese"}
      title={isChinese ? "切换至英文" : "Switch to Chinese"}
    >
      <Languages aria-hidden="true" size={15} className="hidden min-[360px]:block" />
      <span>{isChinese ? "EN" : "中文"}</span>
    </button>
  );
}

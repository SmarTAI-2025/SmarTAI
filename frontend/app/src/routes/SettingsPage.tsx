import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { useI18n } from "@/i18n/I18nProvider";
import { type ThemeMode, useTheme } from "@/theme/ThemeProvider";

const themes: ThemeMode[] = ["light", "dark", "system"];

export function SettingsPage() {
  const { locale, setLocale, t } = useI18n();
  const { theme, setTheme } = useTheme();

  return (
    <div className="grid gap-5">
      <SectionHeader title="设置" description="账号偏好、界面语言、主题与后端连通性。" />
      <Card className="grid gap-4">
        <div>
          <h2 className="text-base font-semibold">{t("language")}</h2>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              type="button"
              variant={locale === "zh-CN" ? "primary" : "secondary"}
              onClick={() => setLocale("zh-CN")}
            >
              中文
            </Button>
            <Button
              type="button"
              variant={locale === "en-US" ? "primary" : "secondary"}
              onClick={() => setLocale("en-US")}
            >
              English
            </Button>
          </div>
        </div>
        <div>
          <h2 className="text-base font-semibold">{t("theme")}</h2>
          <div className="mt-2 flex flex-wrap gap-2">
            {themes.map((item) => (
              <Button
                key={item}
                type="button"
                variant={theme === item ? "primary" : "secondary"}
                onClick={() => setTheme(item)}
              >
                {t(item)}
              </Button>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}


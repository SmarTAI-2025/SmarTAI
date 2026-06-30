import { AlertTriangle, CheckCircle2, RefreshCw, Server } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { backendUrl } from "@/api/client";
import { useHealthStatus } from "@/api/hooks";
import { useI18n } from "@/i18n/I18nProvider";
import { type ThemeMode, useTheme } from "@/theme/ThemeProvider";

const themes: ThemeMode[] = ["light", "dark", "system"];

export function SettingsPage() {
  const { locale, setLocale, t } = useI18n();
  const { theme, setTheme } = useTheme();
  const healthQuery = useHealthStatus({ refetchInterval: 30_000 });
  const isHealthy = healthQuery.data?.status === "healthy";
  const healthLabel = healthQuery.isLoading
    ? t("checking")
    : isHealthy
      ? t("online")
      : t("offline");

  return (
    <div className="grid gap-5">
      <SectionHeader title={t("settings")} description={t("settingsDescription")} />
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
      <Card className="grid gap-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border bg-muted text-muted-foreground">
              <Server aria-hidden="true" size={20} />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold">{t("systemConnection")}</h2>
              <p className="mt-1 break-all text-sm text-muted-foreground">{backendUrl}</p>
            </div>
          </div>
          <Button
            type="button"
            variant="secondary"
            onClick={() => void healthQuery.refetch()}
            disabled={healthQuery.isFetching}
          >
            <RefreshCw
              aria-hidden="true"
              className={healthQuery.isFetching ? "animate-spin" : undefined}
              size={16}
            />
            {t("refresh")}
          </Button>
        </div>

        <div className="grid gap-3 md:grid-cols-4">
          <StatusMetric
            label={t("connectionStatus")}
            value={healthLabel}
            tone={isHealthy ? "success" : healthQuery.isLoading ? "neutral" : "danger"}
          />
          <StatusMetric label={t("engine")} value={healthQuery.data?.engine ?? "-"} />
          <StatusMetric
            label={t("memory")}
            value={
              typeof healthQuery.data?.memory_usage_mb === "number"
                ? `${healthQuery.data.memory_usage_mb.toFixed(2)} MB`
                : "-"
            }
          />
          <StatusMetric
            label={t("cpu")}
            value={
              typeof healthQuery.data?.cpu_percent === "number"
                ? `${healthQuery.data.cpu_percent.toFixed(1)}%`
                : "-"
            }
          />
        </div>

        {healthQuery.isError ? (
          <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
            <AlertTriangle aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
            <p className="min-w-0 break-words">
              {t("backendOfflineHint")}: {formatError(healthQuery.error)}
            </p>
          </div>
        ) : null}

        {isHealthy ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <CheckCircle2 aria-hidden="true" className="text-accent" size={16} />
            <span>{t("backendOnlineHint")}</span>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

function StatusMetric({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "success" | "danger";
}) {
  const toneClass =
    tone === "success"
      ? "text-accent"
      : tone === "danger"
        ? "text-danger"
        : "text-foreground";

  return (
    <div className="rounded-md border bg-background p-3">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className={`mt-1 break-words text-sm font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

function formatError(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Unknown error";
}

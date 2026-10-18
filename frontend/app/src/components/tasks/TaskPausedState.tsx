import { Pause } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import { TASK_ACTION_KEYS, TASK_STAGE_KEYS } from "@/lib/taskPresentation";

export function TaskPausedState({ status, onContinue, onManual, busy, disabled, children }: {
  status: "problems_paused" | "submissions_paused" | "grading_paused";
  onContinue: () => void;
  onManual?: () => void;
  busy?: boolean;
  disabled?: boolean;
  children?: ReactNode;
}) {
  const { t, locale } = useI18n();
  return (
    <section role="status" className="grid justify-items-start gap-4 rounded-[10px] border bg-card p-6 sm:p-10">
      <Pause aria-hidden="true" className="h-7 w-7 text-amber-600" />
      <h2 className="text-2xl font-bold">{t(TASK_STAGE_KEYS[status])}</h2>
      <p className="text-sm leading-6 text-muted-foreground">{t("taskPausedDescription")}</p>
      {children}
      <div className="flex flex-wrap items-center gap-2 max-sm:[&_button]:px-2 max-sm:[&_button]:text-xs!">
        <Button onClick={onContinue} disabled={busy || disabled} aria-busy={busy || undefined}>
          {t(TASK_ACTION_KEYS[status])}
        </Button>
        {onManual ? <Button variant="secondary" onClick={onManual} disabled={busy}>
          {locale === "zh-CN" ? "人工继续处理" : "Continue manually"}
        </Button> : null}
      </div>
    </section>
  );
}

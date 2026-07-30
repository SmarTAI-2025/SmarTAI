import { Check, Circle, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMaterialImport } from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import { materialImportText } from "@/lib/materialImportCopy";

const FACT_STEPS = [
  { key: "source", zh: "准备资料来源", en: "Prepare material source" },
  { key: "match", zh: "匹配题目与资料", en: "Match questions and materials" },
  { key: "prepare", zh: "校验待确认候选", en: "Validate review candidates" },
] as const;

export function QuestionMaterialImportProgressPage() {
  const { taskId, jobId } = useParams();
  const { locale } = useI18n();
  const navigate = useNavigate();
  const importQuery = useMaterialImport(taskId, jobId);
  const plan = importQuery.data;
  const isFailed = plan?.status === "error";
  const hasError = isFailed || importQuery.isError;
  const completedSteps = plan?.progress?.completed_steps;

  useEffect(() => {
    if (!taskId || !jobId || !plan) return;
    if (plan.status === "ready") {
      navigate(`/tasks/${taskId}/questions/import/review/${encodeURIComponent(jobId)}`, { replace: true });
    } else if (plan.status === "applied") {
      navigate(`/tasks/${taskId}/questions`, { replace: true });
    }
  }, [jobId, navigate, plan, taskId]);

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {materialImportText(locale, "progressTitle")}
      </h1>
      <NewTaskStepper currentStep={2} />

      <section className="mx-auto mt-[45px] w-full max-w-[800px] rounded-[10px] border bg-card px-6 py-7 sm:min-h-[390px] sm:px-10 sm:py-9">
        <div className="text-center">
          {hasError ? (
            <Circle aria-hidden="true" className="mx-auto h-8 w-8 text-danger" />
          ) : (
            <LoaderCircle aria-hidden="true" className="mx-auto h-8 w-8 animate-spin text-primary" />
          )}
          <h2 className="mt-4 text-[19px] font-bold leading-6 text-foreground">
            {materialImportText(locale, hasError ? "progressFailedHeading" : "progressHeading")}
          </h2>
          <p className="mx-auto mt-2 max-w-[590px] text-[13px] leading-5 text-muted-foreground">
            {materialImportText(locale, "progressBackground")}
          </p>
        </div>

        <div className="mt-8 grid gap-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <section aria-labelledby="material-import-progress-steps">
            <h3 id="material-import-progress-steps" className="text-[13px] font-semibold text-foreground">
              {materialImportText(locale, "progressSteps")}
            </h3>
            <ol className="mt-3 grid gap-3">
              {FACT_STEPS.map((step, index) => {
                const completed = typeof completedSteps === "number" && completedSteps > index;
                const active = plan?.status === "running" && (
                  typeof completedSteps === "number" ? completedSteps === index : index === 0
                );
                return (
                <li key={step.key} className="flex items-center gap-3 text-[13px] text-muted-foreground">
                  {completed ? <Check className="h-4 w-4 text-accent" /> : active ? <LoaderCircle className="h-4 w-4 animate-spin text-primary" /> : <Circle className="h-4 w-4 text-slate-300" />}
                  <span>{locale === "zh-CN" ? step.zh : step.en}</span>
                </li>
              )})}
            </ol>
          </section>
          <section className="min-w-0" aria-labelledby="material-import-progress-latest">
            <h3 id="material-import-progress-latest" className="text-[13px] font-semibold text-foreground">
              {materialImportText(locale, "progressLatest")}
            </h3>
            <div className="mt-3 min-h-[106px] rounded-[8px] border bg-slate-50 p-4 text-[12px] leading-5 text-muted-foreground dark:bg-slate-900/40">
              {importQuery.isError ? (
                <p className="text-danger">{materialImportText(locale, "progressLoadError")}</p>
              ) : isFailed ? (
                <p className="text-danger">{plan?.progress?.error_detail || plan?.error || materialImportText(locale, "progressLoadError")}</p>
              ) : plan?.progress?.messages?.length ? (
                <ul className="grid gap-1.5">
                  {plan.progress.messages.slice(-4).map((event) => (
                    <li key={`${event.ts}-${event.message}`}>{localizeProgressMessage(event.message, locale)}</li>
                  ))}
                </ul>
              ) : (
                materialImportText(locale, "progressWaiting")
              )}
            </div>
          </section>
        </div>

        <div className="mt-8 flex flex-wrap items-center justify-between gap-3 border-t pt-5">
          <span className="max-w-[420px] truncate text-[11px] text-muted-foreground" title={jobId}>{jobId ?? ""}</span>
          <div className="flex gap-2">
            {hasError ? (
              <Link to={`/tasks/${taskId ?? ""}/questions/import`} className="inline-flex h-9 items-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted">
                {materialImportText(locale, "chooseAgain")}
              </Link>
            ) : (
              <Link to="/" className="inline-flex h-9 items-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted">
                {materialImportText(locale, "backWorkspace")}
              </Link>
            )}
            <button type="button" disabled={importQuery.isFetching} onClick={() => void importQuery.refetch()} className="inline-flex h-9 items-center gap-2 rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted disabled:opacity-50">
              <RefreshCw className="h-4 w-4" />
              {materialImportText(locale, "refresh")}
            </button>
          </div>
        </div>
      </section>
      <span className="sr-only">{taskId}</span>
    </div>
  );
}

function localizeProgressMessage(message: string, locale: "zh-CN" | "en-US") {
  if (locale === "en-US") return message;
  if (message === "Material source prepared") return "资料来源已准备";
  if (message === "Matching source material to known questions") return "正在匹配资料与已有题目";
  if (message === "Validating matched material fields") return "正在校验匹配到的资料字段";
  if (message.startsWith("Prepared ") && message.endsWith(" material candidates for review")) {
    const count = message.match(/\d+/)?.[0] ?? "";
    return `已准备 ${count} 项待确认候选`;
  }
  return message;
}

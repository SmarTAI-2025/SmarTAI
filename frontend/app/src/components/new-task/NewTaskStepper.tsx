import { Check } from "lucide-react";
import { useLayoutEffect, useRef } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useTask } from "@/api/hooks/tasks";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import { canTaskBeRegraded, getTaskReachableStep } from "@/lib/taskFlow";

const STEP_KEYS: MessageKey[] = [
  "newTaskStepTask",
  "newTaskStepUpload",
  "newTaskStepQuestionReview",
  "newTaskStepSubmissions",
  "newTaskStepGradingSetup",
  "newTaskStepGrading",
  "newTaskStepReview",
  "newTaskStepComplete",
];

const STEP_PATHS = [
  "edit",
  "upload/problems",
  "questions",
  "submissions/upload",
  "submissions",
  "grading-setup",
  "review",
  "results",
] as const;

const COMPACT_ENGLISH_LABELS = [
  "New Task",
  "Questions",
  "Question Review",
  "Submissions",
  "Submission Review",
  "Grading",
  "Review Results",
  "Analysis",
] as const;

interface NewTaskStepperProps {
  currentStep?: number;
  reachableStep?: number;
  returnState?: unknown;
  lockedStep?: number;
  lockedStepReason?: string;
  onLockedStepActivate?: () => void;
}

export function NewTaskStepper({
  currentStep = 0,
  reachableStep = currentStep,
  returnState,
  lockedStep,
  lockedStepReason,
  onLockedStepActivate,
}: NewTaskStepperProps) {
  const { locale, t } = useI18n();
  const { taskId } = useParams();
  const taskQuery = useTask(taskId);
  const location = useLocation();
  const navRef = useRef<HTMLElement>(null);
  const currentStepRef = useRef<HTMLLIElement>(null);
  const effectiveReachableStep = Math.max(currentStep, reachableStep, getTaskReachableStep(taskQuery.data));

  useLayoutEffect(() => {
    const nav = navRef.current;
    const current = currentStepRef.current;
    if (!nav || !current || nav.scrollWidth <= nav.clientWidth) return;
    const centeredLeft = current.offsetLeft + current.offsetWidth / 2 - nav.clientWidth / 2;
    nav.scrollLeft = Math.max(0, centeredLeft);
  }, [currentStep, locale]);

  return (
    <nav ref={navRef} aria-label={t("newTaskWorkflow")} className="mt-[14px] overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      <ol className="flex w-max min-w-full items-center">
        {STEP_KEYS.map((key, index) => {
          const fullLabel = t(key);
          const compactLabel = COMPACT_ENGLISH_LABELS[index];

          return (
            <li
              ref={index === currentStep ? currentStepRef : undefined}
              key={key}
              className={`flex min-w-max items-center ${index < STEP_KEYS.length - 1 ? "flex-1" : "shrink-0"}`}
            >
              {index <= effectiveReachableStep && (taskId || index === 0) ? (
                <Link
                  to={stepHref(taskId, index, location.pathname, location.search, taskQuery.data)}
                  state={returnState}
                  aria-current={index === currentStep ? "step" : undefined}
                  aria-label={fullLabel}
                  title={fullLabel}
                  className="group flex shrink-0 items-center gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                >
                  <StepMarker index={index} currentStep={currentStep} />
                  <StepLabel
                    compactLabel={compactLabel}
                    fullLabel={fullLabel}
                    isEnglish={locale === "en-US"}
                    className={`transition-colors group-hover:text-primary ${index === currentStep ? "font-semibold text-foreground" : "text-muted-foreground"}`}
                  />
                </Link>
              ) : index === lockedStep && lockedStepReason && onLockedStepActivate ? (
                <button
                  type="button"
                  title={lockedStepReason}
                  aria-label={`${fullLabel}${locale === "zh-CN" ? "：" : ": "}${lockedStepReason}`}
                  onClick={onLockedStepActivate}
                  className="group flex shrink-0 items-center gap-2 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                >
                  <StepMarker index={index} currentStep={currentStep} />
                  <StepLabel
                    compactLabel={compactLabel}
                    fullLabel={fullLabel}
                    isEnglish={locale === "en-US"}
                    className="text-muted-foreground transition-colors group-hover:text-foreground"
                  />
                  <span className="sr-only">{lockedStepReason}</span>
                </button>
              ) : (
                <div
                  className="flex shrink-0 cursor-not-allowed items-center gap-2"
                  aria-disabled="true"
                  title={fullLabel}
                >
                  <StepMarker index={index} currentStep={currentStep} />
                  <StepLabel
                    compactLabel={compactLabel}
                    fullLabel={fullLabel}
                    isEnglish={locale === "en-US"}
                    className="text-muted-foreground"
                  />
                </div>
              )}
              {index < STEP_KEYS.length - 1 ? (
                <span
                  aria-hidden="true"
                  data-step-connector
                  className={`mx-1.5 h-px min-w-2 flex-1 ${index < currentStep ? "bg-accent" : "bg-border"}`}
                />
              ) : null}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function StepLabel({
  className,
  compactLabel,
  fullLabel,
  isEnglish,
}: {
  className: string;
  compactLabel: string;
  fullLabel: string;
  isEnglish: boolean;
}) {
  return (
    <span className={`whitespace-nowrap text-[13px] font-medium ${className}`}>
      {isEnglish ? (
        <>
          <span className="xl:hidden">{compactLabel}</span>
          <span className="hidden xl:inline">{fullLabel}</span>
        </>
      ) : (
        fullLabel
      )}
    </span>
  );
}

function stepHref(
  taskId: string | undefined,
  index: number,
  pathname: string,
  search: string,
  task?: Parameters<typeof getTaskReachableStep>[0],
) {
  if (!taskId) return "/tasks/new";
  if (index === 0) {
    if (pathname.endsWith("/edit")) return `${pathname}${search}`;
    const returnTo = `${pathname}${search}`;
    return `/tasks/${encodeURIComponent(taskId)}/edit?returnTo=${encodeURIComponent(returnTo)}`;
  }
  if (index === 5 && canTaskBeRegraded(task?.status)) {
    return `/tasks/${encodeURIComponent(taskId)}/grading-setup`;
  }
  if (index === 5 && pathname.endsWith("/grading/progress")) return `${pathname}${search}`;
  if (index === 5 && pathname.endsWith("/grading/preflight")) return `${pathname}${search}`;
  if (index === 5 && (task?.status === "grading" || (task?.status === "error" && task.grading_job_id))) {
    return `/tasks/${encodeURIComponent(taskId)}/grading/progress`;
  }
  if (index === 5 && task?.status === "generating_analysis") {
    return `/tasks/${encodeURIComponent(taskId)}/grading/preflight`;
  }
  return `/tasks/${encodeURIComponent(taskId)}/${STEP_PATHS[index]}`;
}

function StepMarker({ index, currentStep }: { index: number; currentStep: number }) {
  return (
    <span
      className={`inline-flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
        index < currentStep
          ? "bg-accent text-white"
          : index === currentStep
            ? "bg-primary text-primary-foreground"
            : "bg-slate-200 text-white dark:bg-slate-600"
      }`}
    >
      {index < currentStep ? <Check aria-hidden="true" size={15} strokeWidth={3} /> : index + 1}
    </span>
  );
}

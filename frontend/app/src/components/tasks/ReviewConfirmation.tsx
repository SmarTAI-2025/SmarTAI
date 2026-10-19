import { userFacingError } from "@/lib/taskActionGuards";
import { ArrowRight, Check, LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useStartGrading } from "@/api/hooks/tasks";
import { getAPIErrorCode } from "@/api/client";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { cn } from "@/lib/cn";
import type { ReviewBlocker } from "@/lib/reviewConfirmation";

export const reviewActionClass = "inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-lg border px-4 text-sm font-semibold outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
type ReviewNavigationAction = { href?: string; label: string; onClick?: () => void; disabled?: boolean; busy?: boolean; id?: string };

/** The three review stages share one action order and one set of control styles. */
export function ReviewStageActions({ confirmation, back, next, note, leftAction, children, className }: {
  confirmation?: ReactNode;
  back: ReviewNavigationAction;
  next: ReviewNavigationAction;
  note?: ReactNode;
  leftAction?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return <footer className={cn("mt-4 rounded-[10px] border bg-card px-4 py-3 xl:px-5", className)}>
    <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
      {note || leftAction ? <div className="flex min-w-0 items-center gap-4" data-review-stage-note>
        {note ? <p className="text-xs leading-5 text-muted-foreground">{note}</p> : null}{leftAction}
      </div> : null}
      <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap sm:justify-end xl:ml-auto" data-review-stage-actions>
        {confirmation}
        <ReviewNavigationButton action={back} />
        <ReviewNavigationButton action={next} primary />
      </div>
    </div>
    {children}
  </footer>;
}

function ReviewNavigationButton({ action, primary = false }: { action: ReviewNavigationAction; primary?: boolean }) {
  const className = cn(reviewActionClass, primary
    ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
    : "bg-card text-foreground");
  const content = <>{action.label}{primary ? action.busy
    ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
    : <ArrowRight className="h-4 w-4" aria-hidden="true" /> : null}</>;
  if (action.href && !action.disabled && !action.busy && !action.onClick) {
    return <Link id={action.id} to={action.href} className={className}>{content}</Link>;
  }
  return <button id={action.id} type="button" className={className} disabled={action.disabled || action.busy} onClick={action.onClick}>{content}</button>;
}
export function ReviewConfirmButton({ locale, confirmed = false, blocked = false, busy = false, disabled = false, compact = false, all = false, save = false, onClick, title }: {
  locale: string; confirmed?: boolean; blocked?: boolean; busy?: boolean; disabled?: boolean; compact?: boolean; all?: boolean; save?: boolean; onClick: () => void; title?: string;
}) {
  const zh = locale === "zh-CN";
  const label = busy ? (zh ? "确认中" : "Confirming") : confirmed ? (zh ? "已确认" : "Confirmed") : all ? (zh ? "全部确认" : "Confirm all") : save ? (zh ? "保存并确认" : "Save and confirm") : (zh ? "待确认" : "Confirm review");
  return <button type="button" onClick={onClick} disabled={disabled || busy || confirmed} title={title} aria-label={title ? `${title} · ${label}` : label} className={cn(reviewActionClass, compact && "h-7 rounded-full px-3 text-xs", confirmed ? "border-emerald-200 bg-emerald-100 text-emerald-800 disabled:opacity-100 dark:bg-emerald-950/40 dark:text-emerald-200" : blocked ? "border-red-200 bg-red-50 text-red-700" : "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 dark:bg-emerald-950/40 dark:text-emerald-200")}>
    {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Check className="h-4 w-4" aria-hidden="true" />}{label}
  </button>;
}
export function ReviewBlockDialog({ locale, issues, onClose }: { locale: string; issues: ReviewBlocker[]; onClose: () => void }) {
  const navigate = useNavigate();
  if (!issues.length) return null;
  const zh = locale === "zh-CN";
  return <UnsavedChangesDialog title={zh ? "请先处理关键问题" : "Resolve required items first"}
    description={zh ? "请在对应位置补全或修正关键问题，并确认复核后继续。识别或批改失败时可仅重试失败项；学生实际留白不受此限制。" : "Correct the required items at the linked locations, then confirm your review to continue. Retry only failed recognition or grading items when needed. Genuine blank responses can be confirmed."}
    stayLabel={zh ? "返回" : "Back"} leaveLabel={zh ? "前往问题位置" : "Go to issue"} onStay={onClose} onLeave={() => { onClose(); navigate(issues[0].href); }}>
    <ul className="mt-3 max-h-64 space-y-2 overflow-auto text-sm">{issues.map((item, index) => <li key={`${item.href}-${index}`}><Link onClick={onClose} className="text-primary underline underline-offset-2" to={item.href}>{item.label}</Link></li>)}</ul>
  </UnsavedChangesDialog>;
}
export function RetryFailedGrading({ taskId, revision, locale, paused = false }: { taskId: string; revision: number; locale: string; paused?: boolean }) {
  const retry = useStartGrading(); const navigate = useNavigate(); const zh = locale === "zh-CN";
  return <div className="space-y-2"><button type="button" className={reviewActionClass} disabled={retry.isPending} onClick={() => retry.mutate({ taskId, expectedWorkflowRevision: revision, requestId: `retry-${crypto.randomUUID()}`, retryScope: "failed_only" }, { onSuccess: (data) => { if (data.status === "started" || data.status === "already_running") navigate(`/tasks/${taskId}/grading/progress`); } })}>{retry.isPending ? (zh ? "正在启动" : "Starting") : paused ? (zh ? "继续未完成批改" : "Resume incomplete grading") : (zh ? "仅重试失败项" : "Retry failed items")}</button>
    <p className="text-xs text-muted-foreground">{paused ? (zh ? "仅继续未完成的模型批改并使用模型额度；已保存的有效评分、教师分数和评语保留。也可在复核队列中人工补齐并确认。" : "Uses model quota only for unfinished grading. Saved valid grades and teacher edits are retained. You can also complete and confirm items manually in the review queue.") : (zh ? "仅重新批改缺少有效分数的题次并使用模型额度；成功结果、教师分数和评语保留。若仍失败，请手动给分后确认。" : "Uses model quota only for results without valid scores. Successful results and teacher scores/comments are retained. If retry still fails, enter a score before confirming.")}</p>
    {retry.isError ? <p role="alert" className="text-sm text-red-700">{getAPIErrorCode(retry.error) === "grading_inputs_changed" ? (zh ? "题目、作答或模型配置已改变。请返回批改设置核对后启动新的批改。" : "Questions, answers or model configuration changed. Review grading setup before starting a new run.") : getAPIErrorCode(retry.error) === "grading_retry_unavailable" ? (zh ? "当前没有可重试的失败批改，请刷新页面。" : "No failed grading run is available. Refresh the page.") : userFacingError(retry.error, locale)}</p> : null}</div>;
}

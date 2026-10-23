import { userFacingError } from "@/lib/taskActionGuards";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTask } from "@/api/hooks/tasks";
import { getAPIErrorCode } from "@/api/client";
import { continueTaskRun, manuallyCompleteQuestionPreparation, manuallyCompleteSubmissionRecognition } from "@/api/tasks";
import { useI18n } from "@/i18n/I18nProvider";
import { Button } from "@/components/ui/Button";
import { manualReviewDestination } from "@/lib/taskFlow";
import { ManualHandoffState } from "./ManualHandoffState";

type RecoveryTask = Pick<import("@/types").Task, "status" | "error" | "workflow_revision" | "last_failed_job_id" | "grading_job_id" | "parse_job_id" | "extract_job_id">;
type Stage = "questions" | "submissions" | "grading";
type Props = { taskId?: string | null; task?: RecoveryTask | null; stage?: Stage };

/** Open persisted review rows without starting another model operation. */
export function ManualReviewButton(props: Props) {
  // Most entry pages already have the authoritative task; avoid another read.
  return props.task ? <LoadedManualReviewButton {...props} task={props.task} /> : null;
}

/** Settings has only a setup DTO; fetch task metadata only for a failed run. */
export function ManualReviewLoader(props: Omit<Props, "task">) {
  const { data: task } = useTask(props.taskId ?? undefined);
  return <ManualReviewButton {...props} task={task} />;
}

function LoadedManualReviewButton({ taskId, task, stage }: Props & { task: RecoveryTask }) {
  const jobId = task.last_failed_job_id;
  const failedStage = jobId && (jobId === task.grading_job_id ? "grading" : jobId === task.parse_job_id ? "submissions" : jobId === task.extract_job_id ? "questions" : null);
  if (!taskId || !jobId || !failedStage || (stage && stage !== failedStage)
      || !["error", "problems_paused", "submissions_paused", "grading_paused"].includes(task.status)) return null;
  return <ManualReviewAction key={jobId} taskId={taskId} task={task} jobId={jobId} failedStage={failedStage} />;
}

function ManualReviewAction({ taskId, task, jobId, failedStage }: { taskId: string; task: RecoveryTask; jobId: string; failedStage: Stage }) {
  const { locale } = useI18n();
  const navigate = useNavigate();
  const cache = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retired, setRetired] = useState(false);
  const unavailable = retired || task.error === "saved_text_recovery_retired";
  async function open() {
    if (busy || unavailable || !taskId || !task || !jobId) return;
    setBusy(true); setError("");
    try {
      let href: string;
      if (failedStage === "questions") {
        const result = await manuallyCompleteQuestionPreparation({ taskId, jobId, expectedWorkflowRevision: task.workflow_revision });
        href = manualReviewDestination(taskId, failedStage, result.status);
      } else if (failedStage === "submissions") {
        const result = await manuallyCompleteSubmissionRecognition({ taskId, jobId, expectedWorkflowRevision: task.workflow_revision });
        href = manualReviewDestination(taskId, failedStage, result.status);
      } else {
        const result = await continueTaskRun(taskId, jobId, task.workflow_revision, true);
        href = manualReviewDestination(taskId, failedStage, result.status);
      }
      await cache.invalidateQueries({ queryKey: ["tasks"] });
      navigate(href);
    } catch (caught) { setError(userFacingError(caught, locale)); setRetired(getAPIErrorCode(caught) === "saved_text_recovery_retired"); }
    finally { setBusy(false); }
  }
  if (busy) return <div className="mt-3"><ManualHandoffState stage={failedStage} locale={locale} /></div>;
  if (unavailable) return <div className="mt-3"><p role="alert" className="mb-2 text-xs text-muted-foreground">{error || userFacingError("saved_text_recovery_retired", locale)}</p><Button variant="secondary" onClick={() => navigate("/tasks/new")}>{locale === "zh-CN" ? "新建任务" : "Create a new task"}</Button></div>;
  return <div className="mt-3"><Button variant="secondary" disabled={busy} onClick={() => void open()}>{locale === "zh-CN" ? "人工继续处理" : "Continue manually"}</Button>{error ? <p role="alert" className="mt-2 text-xs text-red-700">{error}</p> : null}</div>;
}

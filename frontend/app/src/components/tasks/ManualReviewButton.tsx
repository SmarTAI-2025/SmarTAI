import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTask } from "@/api/hooks/tasks";
import { continueTaskRun, manuallyCompleteQuestionPreparation, manuallyCompleteSubmissionRecognition } from "@/api/tasks";
import { normalizeAPIError } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { Button } from "@/components/ui/Button";

type RecoveryTask = Pick<import("@/types").Task, "status" | "workflow_revision" | "last_failed_job_id" | "grading_job_id" | "parse_job_id" | "extract_job_id">;
type Stage = "questions" | "submissions" | "grading";
type Props = { taskId?: string | null; task?: RecoveryTask | null; stage?: Stage };

/** Recovery does not require an available model or resubmit a provider request. */
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
  return <ManualReviewAction taskId={taskId} task={task} jobId={jobId} failedStage={failedStage} />;
}

function ManualReviewAction({ taskId, task, jobId, failedStage }: { taskId: string; task: RecoveryTask; jobId: string; failedStage: Stage }) {
  const { locale } = useI18n();
  const navigate = useNavigate();
  const cache = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function open() {
    if (busy || !taskId || !task || !jobId) return;
    setBusy(true); setError("");
    try {
      let href: string;
      if (failedStage === "questions") {
        const result = await manuallyCompleteQuestionPreparation({ taskId, jobId, expectedWorkflowRevision: task.workflow_revision });
        href = result.first_question_id ? `/tasks/${taskId}/questions` : `/tasks/${taskId}/questions?manualJob=${encodeURIComponent(jobId)}`;
      } else if (failedStage === "submissions") {
        await manuallyCompleteSubmissionRecognition({ taskId, jobId, expectedWorkflowRevision: task.workflow_revision });
        href = `/tasks/${taskId}/submissions`;
      } else {
        await continueTaskRun(taskId, jobId, task.workflow_revision, true);
        href = `/tasks/${taskId}/review`;
      }
      await cache.invalidateQueries({ queryKey: ["tasks"] });
      navigate(href);
    } catch (caught) { setError(normalizeAPIError(caught).message); }
    finally { setBusy(false); }
  }
  return <div className="mt-3"><Button variant="secondary" disabled={busy} onClick={() => void open()}>{locale === "zh-CN" ? "人工继续处理" : "Continue manually"}</Button>{error ? <p role="alert" className="mt-2 text-xs text-red-700">{error}</p> : null}</div>;
}

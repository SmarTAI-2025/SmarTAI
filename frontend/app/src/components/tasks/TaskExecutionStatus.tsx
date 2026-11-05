import { Link } from "react-router-dom";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useState, useRef } from "react";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { getAPIErrorCode } from "@/api/client";
import { continueTaskRun } from "@/api/tasks";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import { TaskExecutionControls } from "./TaskExecutionControls";

/** Job-specific pages must never offer to stop a newer run of the same task. */
export function TaskExecutionStatus({ taskId, jobId, onChanged }: {
  taskId?: string; jobId?: string; onChanged: () => Promise<unknown>;
}) {
  const query = useTaskProgress(taskId, { enabled: Boolean(jobId) });
  const { locale } = useI18n();
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState(false);
  const [acknowledgedKey, setAcknowledgedKey] = useState<string | null>(null);
  const [uncertainKey, setUncertainKey] = useState<string | null>(null);
  const busy = useRef(false);
  const refresh = () => Promise.all([query.refetch(), onChanged()]);
  const code = query.data?.error;
  if (jobId && query.data?.last_failed_job_id === jobId && ["operation_cancelled", "question_preparation_time_limit_reached", "question_preparation_wall_time_limit_reached", "provider_submit_uncertain"].includes(code ?? "")) {
    const zh = locale === "zh-CN";
    const recoveryKey = `${jobId}:${query.data.workflow_revision}:${code}`;
    const needsAcknowledgment = code === "question_preparation_wall_time_limit_reached" || code === "provider_submit_uncertain" || uncertainKey === recoveryKey;
    const acknowledged = acknowledgedKey === recoveryKey;
    const info = classifyRecoverableError(code, { locale });
    return <div className="mb-5 rounded-lg border bg-card p-4" role="status">
      <p className="font-medium">{code === "operation_cancelled" ? (zh ? "本次运行已停止" : "This run has stopped") : info.title}</p>
      <p className="my-2 text-sm text-muted-foreground">{zh ? "按原配置继续处理，保留已保存结果，并按新的提交时间重新排队。未完成的模型请求可能再次计费。" : "Continue with saved settings and results, rejoining the queue with a new submission time. Unfinished model requests may be billed again."}</p>
      {code !== "operation_cancelled" && <p className="my-2 text-sm text-muted-foreground">{info.description}</p>}
      {needsAcknowledgment && <label className="my-3 flex items-start gap-2 text-sm">
        <input type="checkbox" checked={acknowledged} disabled={resuming}
          onChange={(event) => setAcknowledgedKey(event.target.checked ? recoveryKey : null)} />
        {zh ? "我了解未完成的模型请求可能再次计费，确认继续" : "I understand unfinished model requests may be billed again and confirm continuing"}
      </label>}
      <Button disabled={resuming || (needsAcknowledgment && !acknowledged)} onClick={async () => {
        if (busy.current || !taskId || !query.data || (needsAcknowledgment && !acknowledged)) return;
        busy.current = true; setResuming(true); setError(false);
        try { await continueTaskRun(taskId, jobId, query.data.workflow_revision, false, code === "operation_cancelled" || (needsAcknowledgment && acknowledged)); setAcknowledgedKey(null); await refresh(); }
        catch (failure) { if (getAPIErrorCode(failure) === "provider_submit_uncertain") setUncertainKey(recoveryKey); setError(true); }
        finally { busy.current = false; setResuming(false); }
      }}>{zh ? "继续处理" : "Continue processing"}</Button>
      {taskId ? <Link to={`/tasks/${taskId}/questions`} className="ml-2 inline-flex h-9 items-center rounded-lg border px-4 text-sm font-semibold">{zh ? "人工继续处理" : "Continue manually"}</Link> : null}
      {error && <p role="alert">{zh ? "未能继续，请刷新状态后重试。" : "Could not continue. Refresh the status and retry."}</p>}
    </div>;
  }
  if (!jobId || query.data?.active_job_id !== jobId) return null;
  return <TaskExecutionControls taskId={taskId} state={query.data} progress={query.progress}
    onChanged={refresh} />;
}

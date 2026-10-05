import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useState, useRef } from "react";
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
  const busy = useRef(false);
  const refresh = () => Promise.all([query.refetch(), onChanged()]);
  if (jobId && query.data?.last_failed_job_id === jobId && query.data.error === "operation_cancelled") {
    const zh = locale === "zh-CN";
    return <div className="mb-5 rounded-lg border bg-card p-4" role="status">
      <p className="font-medium">{zh ? "本次运行已停止" : "This run has stopped"}</p>
      <p className="my-2 text-sm text-muted-foreground">{zh ? "按原配置继续处理，保留已保存结果，并按新的提交时间重新排队。未完成的模型请求可能再次计费。" : "Continue with saved settings and results, rejoining the queue with a new submission time. Unfinished model requests may be billed again."}</p>
      <Button disabled={resuming} onClick={async () => {
        if (busy.current || !taskId || !query.data) return;
        busy.current = true; setResuming(true); setError(false);
        try { await continueTaskRun(taskId, jobId, query.data.workflow_revision); await refresh(); }
        catch { setError(true); }
        finally { busy.current = false; setResuming(false); }
      }}>{zh ? "继续处理" : "Continue processing"}</Button>
      {error && <p role="alert">{zh ? "未能继续，请刷新状态后重试。" : "Could not continue. Refresh the status and retry."}</p>}
    </div>;
  }
  if (!jobId || query.data?.active_job_id !== jobId) return null;
  return <TaskExecutionControls taskId={taskId} state={query.data} progress={query.progress}
    onChanged={refresh} />;
}

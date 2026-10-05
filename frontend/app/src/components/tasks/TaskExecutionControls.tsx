import { useEffect, useRef, useState } from "react";
import { stopTaskRun } from "@/api/tasks";
import { Button } from "@/components/ui/Button";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useI18n } from "@/i18n/I18nProvider";
import type { JobProgress, TaskStateSnapshot } from "@/types";

export function TaskExecutionControls({ taskId, state, progress, onChanged }: {
  taskId?: string; state?: TaskStateSnapshot; progress?: JobProgress | null;
  onChanged: () => Promise<unknown>;
}) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const [confirming, setConfirming] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const busy = useRef(false);
  const waits = progress?.model_waits ?? [];
  const jobId = state?.active_job_id;
  useEffect(() => {
    if (!waits.length) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [waits.length]);
  useEffect(() => { setConfirming(false); setError(false); }, [jobId]);
  if (!taskId || !jobId || !state) return null;
  const queued = state.active_operation_status === "pending";

  async function stop() {
    if (busy.current || !taskId || !jobId || !state) return;
    busy.current = true; setStopping(true); setError(false);
    try {
      await stopTaskRun(taskId, jobId, state.workflow_revision);
      await onChanged();
      setConfirming(false);
    } catch { setError(true); }
    finally { busy.current = false; setStopping(false); }
  }
  return <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-4">
    <div className="min-w-0 flex-1" role="status">
      {queued ? <>
        <p className="font-medium">{zh ? "排队中" : "Queued"}</p>
        <p className="mt-1 text-sm text-muted-foreground">{state.queue_reason === "user_busy"
          ? (zh ? "你已有任务正在处理，本任务排队中。" : "You already have a task running. This task is queued.")
          : (zh ? "服务器繁忙，本任务已自动排队。" : "The server is busy. This task has been queued.")}
          {zh ? "可以离开页面或退出登录，已提交的任务会继续在后台处理。" : "You can leave this page or sign out. Submitted work continues in the background."}</p>
      </> : waits.length ? waits.map((wait, index) => {
        const seconds = Math.max(0, Math.ceil(wait.retry_at - now));
        return <div key={`${wait.model}:${index}`} className="text-sm text-amber-700 dark:text-amber-300">
          <p className="font-semibold">{zh ? "服务商限流，等待重试" : "Provider rate limit — retry pending"}</p>
          <p>{wait.model} · {seconds > 0
            ? (zh ? `已尝试 ${wait.attempt - 1}/${wait.max_attempts} 次，${seconds} 秒后重试。` : `${wait.attempt - 1}/${wait.max_attempts} attempts used. Retrying in ${seconds}s.`)
            : (zh ? `正在进行第 ${wait.attempt}/${wait.max_attempts} 次尝试。` : `Attempt ${wait.attempt}/${wait.max_attempts} is running.`)}</p>
          <p>{zh ? "达到上限后自动停止；你也可以现在停止。" : "Stops at the attempt limit. You can also stop now."}</p>
        </div>;
      }) : <p className="text-sm text-muted-foreground">{zh ? "任务正在后台处理" : "Task running in the background"}</p>}
    </div>
    <Button variant="secondary" disabled={stopping} onClick={() => setConfirming(true)}>
      {stopping ? (zh ? "正在停止…" : "Stopping…") : (zh ? "停止运行" : "Stop run")}
    </Button>
    {confirming && <UnsavedChangesDialog
      title={zh ? "停止本次运行？" : "Stop this run?"}
      description={zh ? "保留配置、原文件和已保存结果。已发出的模型请求可能仍会计费。继续处理时按新的提交时间重新排队。" : "Settings, original files and saved results are kept. Requests already sent may still be billed. Continuing rejoins the queue with a new submission time."}
      stayLabel={zh ? "继续等待" : "Keep waiting"}
      leaveLabel={stopping ? (zh ? "正在停止…" : "Stopping…") : (zh ? "停止运行" : "Stop run")}
      saving={stopping} onStay={() => { if (!stopping) setConfirming(false); }} onLeave={() => void stop()}
    >{error && <p role="alert" className="text-sm text-destructive">{zh ? "未能确认停止，请刷新状态后重试。" : "Could not confirm the stop. Refresh the status and try again."}</p>}</UnsavedChangesDialog>}
  </div>;
}

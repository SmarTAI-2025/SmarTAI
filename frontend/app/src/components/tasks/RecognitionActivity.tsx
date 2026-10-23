import { useEffect, useRef, useState, type ReactNode } from "react";
import type { JobProgress, ProgressEvent, RecognitionRequestState } from "@/types/progress";
import type { Locale } from "@/i18n/messages";

export function recognitionEventText(event: ProgressEvent | undefined, zh: boolean): string {
  const labels: Record<string, string> = {
    saved_text_recovery_retired: zh ? "此旧整理任务已停用，请新建任务" : "This older organization task is retired. Create a new task",
    question_preparation_artifact_invalid: zh ? "题目结果保存或恢复未完成，已保存的识别内容仍保留" : "Question results could not be saved or restored; saved recognition content is retained",
    question_preparation_artifact_conflict: zh ? "题目结果保存校验未通过，已保存的识别内容仍保留" : "Question results did not pass save checks; saved recognition content is retained",
    activity_preparing: zh ? "正在准备文件" : "Preparing files",
    activity_reading: zh ? "正在读取文件内容" : "Reading file content",
    activity_recognizing: zh ? "正在识别并整理内容" : "Recognizing and organizing content",
    activity_materials: zh ? "正在准备题目资料" : "Preparing question materials",
    activity_saved: zh ? "正在整理和保存结果" : "Organizing and saving results",
    activity_completed: zh ? "本次识别已完成" : "Recognition completed",
    activity_failed: zh ? "一项内容未完成，请查看失败提示" : "An item did not finish; see the failure details",
    activity_stopped: zh ? "本次识别已停止" : "Recognition stopped",
    operation_cancelled: zh ? "本次识别已停止" : "Recognition stopped",
    activity_queued: zh ? "等待请求名额" : "Waiting for request capacity",
    activity_responding: zh ? "请求已发出，等待模型响应" : "Request sent; awaiting model response",
    activity_retrying: zh ? "正在等待重试" : "Awaiting retry",
    activity_received: zh ? "一项内容已处理" : "An item has been processed",
    activity_response_received: zh ? "模型已返回，正在整理结果" : "Model response received; organizing the result",
  };
  // No raw-message fallback, including for older servers or unknown events.
  const text = event && labels[event.message];
  const detail = event?.recognition;
  const fallback = event?.level === "error" ? (zh ? "识别未完成，请查看失败提示" : "Recognition did not finish; see the failure details") : (zh ? "等待进度更新" : "Waiting for a progress update");
  return `${text ?? fallback}${detail?.state === "retrying" ? retryText(detail, zh) : ""}`;
}

function retryText(request: RecognitionRequestState, zh: boolean) {
  const reasons = { rate_limit: zh ? "请求受限" : "Rate limited", timeout: zh ? "响应超时" : "Response timed out", unavailable: zh ? "请求暂未成功" : "Request temporarily unsuccessful" };
  return `${request.reason ? ` · ${reasons[request.reason]}` : ""}${request.attempt && request.max_attempts ? (zh ? ` · 第 ${request.attempt}/${request.max_attempts} 次尝试` : ` · Attempt ${request.attempt}/${request.max_attempts}`) : ""}`;
}

export function RecognitionRequestStatus({ progress, locale, stale = false }: { progress: JobProgress | null; locale: Locale; stale?: boolean }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const zh = locale === "zh-CN";
  const terminal = progress?.phase === "paused" || progress?.phase === "done" || progress?.phase === "error";
  const requests = terminal ? [] : progress?.recognition_requests ?? [];
  // Older snapshots still carry factual rate-limit waits.
  const retries = requests.filter((r) => r.state === "retrying");
  if (progress?.recognition_requests === undefined && !terminal) retries.push(...(progress?.model_waits ?? []).map((wait) => ({ ...wait, state: "retrying" as const, reason: "rate_limit" as const })));
  const waiting = requests.filter((r) => r.state === "queued").reduce((sum, r) => sum + (r.count ?? 1), 0);
  const responding = requests.filter((r) => r.state === "responding").reduce((sum, r) => sum + (r.count ?? 1), 0);
  const groups = new Map<string, { request: RecognitionRequestState; count: number }>();
  for (const request of retries) {
    const key = `${request.reason}:${request.attempt}:${request.max_attempts}`;
    const group = groups.get(key);
    if (group) {
      group.count += request.count ?? 1;
      if ((request.retry_at ?? Infinity) < (group.request.retry_at ?? Infinity)) group.request = request;
    } else groups.set(key, { request, count: request.count ?? 1 });
  }
  useEffect(() => {
    if ((!requests.length && !retries.length) || stale) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [requests.length, retries.length, stale]);
  if (stale) return <p role="status" className="mt-3 text-sm text-muted-foreground">{zh ? "暂时无法更新状态，保留上次进度" : "Status updates are unavailable; showing the last progress"}</p>;
  if (!waiting && !responding && !retries.length) return null;
  return <div role="status" className="mt-3 space-y-1 text-xs leading-5 text-muted-foreground">
    {(waiting > 0 || responding > 0) && <p>{[
      waiting ? (zh ? `${waiting} 个请求等待名额` : `${waiting} requests waiting for capacity`) : "",
      responding ? (zh ? `${responding} 个请求等待模型响应` : `${responding} requests awaiting model response`) : "",
    ].filter(Boolean).join(" · ")}</p>}
    {[...groups].map(([key, { request, count }]) => {
      const seconds = request.retry_at ? Math.max(0, Math.ceil(request.retry_at - now)) : null;
      return <p key={key}>{zh ? `${count} 个请求等待重试` : `${count} requests awaiting retry`}{retryText(request, zh)}{seconds && seconds > 0 ? (zh ? ` · 最近一次将在 ${seconds} 秒后重试` : ` · Next retry in ${seconds}s`) : (zh ? " · 等待下一次状态更新" : " · Waiting for the next status update")}</p>;
    })}
  </div>;
}

/** Uses the existing bounded, durable progress messages; no new polling API. */
export function RecognitionActivity({ progress, locale, incomplete = false, fill = false, summary }: { progress: JobProgress | null; locale: Locale; incomplete?: boolean; fill?: boolean; summary?: ReactNode }) {
  const list = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const zh = locale === "zh-CN";
  const events = progress?.messages ?? [];
  const latest = events.at(-1);
  const latestText = incomplete ? (progress?.error_detail === "operation_cancelled"
    ? (zh ? "本次识别已停止" : "Recognition stopped")
    : (zh ? "本次识别未完成" : "Recognition did not complete")) : recognitionEventText(latest, zh);
  useEffect(() => { stickToBottom.current = true; }, [progress?.job_id]);
  useEffect(() => {
    if (stickToBottom.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [events, progress?.job_id]);
  return <section className={`${fill ? "absolute inset-0" : "mt-3 h-64"} flex min-h-0 flex-col overflow-hidden rounded-lg border bg-slate-50 px-5 py-4 text-xs text-muted-foreground dark:bg-slate-900/30`}>
    <h3 className="mb-2 shrink-0 font-semibold text-foreground">{zh ? "最近动态" : "Recent activity"}</h3>
    {summary && <div className="mb-2 shrink-0 leading-5">{summary}</div>}
    <p className={incomplete ? "mb-2 shrink-0" : "sr-only"} aria-live="polite">{latestText}</p>
    <div ref={list} className="min-h-0 flex-1 overflow-y-auto overscroll-contain focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" role="region" aria-label={zh ? "识别动态历史" : "Recognition activity history"} tabIndex={0}
      onScroll={() => { if (list.current) stickToBottom.current = list.current.scrollHeight - list.current.scrollTop - list.current.clientHeight < 32; }}>
      <p className="mb-2">{zh ? "显示当前保存的最近动态，最多 200 条" : "Showing the saved recent activity, up to 200 entries"}</p>
      <ol className="space-y-2">{events.map((event, index) => <li key={`${event.ts}:${index}`} className="flex gap-3 leading-5">
        <time className="shrink-0 tabular-nums">{Number.isFinite(event.ts) && event.ts > 0 ? new Date(event.ts * (event.ts > 1e12 ? 1 : 1000)).toLocaleTimeString(locale) : "—"}</time>
        <span className="min-w-0 break-words">{recognitionEventText(event, zh)}</span>
      </li>)}</ol>
      {!events.length && <p>{zh ? "暂无动态记录" : "No activity recorded yet"}</p>}
    </div>
  </section>;
}

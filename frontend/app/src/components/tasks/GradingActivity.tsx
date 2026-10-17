import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { getGradingActivity } from "@/api/tasks";
import type { GradingActivityEvent, GradingModelProgress, JobProgress } from "@/types/progress";
import type { Locale } from "@/i18n/messages";

export function ModelProgressRows({ models, locale, stopped = false }: {
  models: GradingModelProgress[]; locale: Locale; stopped?: boolean;
}) {
  const zh = locale === "zh-CN";
  return <div className="mt-3 space-y-1 text-[13px] leading-5 text-muted-foreground" aria-label={zh ? "各模型进度" : "Model progress"}>
    {models.map((model) => {
      const remaining = Math.max(0, model.total - model.succeeded - model.failed);
      const pending = Math.max(0, remaining - model.processing - model.queued - model.responding - model.retrying);
      const details = stopped ? [zh ? `未完成 ${remaining}` : `${remaining} unfinished`] : [
        model.processing ? (zh ? `处理中 ${model.processing}` : `${model.processing} processing`) : "",
        model.queued ? (zh ? `等待请求名额 ${model.queued}` : `${model.queued} waiting for capacity`) : "",
        model.responding ? (zh ? `等待模型响应 ${model.responding}` : `${model.responding} awaiting response`) : "",
        model.retrying ? (zh ? `等待重试 ${model.retrying}` : `${model.retrying} awaiting retry`) : "",
        pending ? (zh ? `待处理 ${pending}` : `${pending} pending`) : "",
      ];
      return <p key={model.id} className="break-words">
        <span className="font-medium text-foreground">{model.label}</span>
        {zh ? `：已处理 ${model.succeeded + model.failed} / ${model.total}，有效结果 ${model.succeeded}`
          : `: processed ${model.succeeded + model.failed} / ${model.total}, valid results ${model.succeeded}`}
        {model.failed > 0 ? (zh ? `，失败 ${model.failed}` : `, failed ${model.failed}`) : ""}
        {details.filter(Boolean).length ? ` · ${details.filter(Boolean).join(" · ")}` : ""}
      </p>;
    })}
  </div>;
}

export function gradingActivityText(event: GradingActivityEvent | null | undefined, models: GradingModelProgress[], zh: boolean) {
  if (!event) return zh ? "等待进度更新" : "Waiting for a progress update";
  const label = models.find((model) => model.id === event.model_id)?.label;
  const labels = {
    processing: zh ? "正在处理作答" : "Processing an answer",
    queued: zh ? "等待请求名额" : "Waiting for request capacity",
    responding: zh ? "请求已发出，等待模型响应" : "Request sent; awaiting model response",
    retrying: zh ? "等待重试" : "Awaiting retry",
    succeeded: zh ? "已获得一份有效结果" : "Received a valid result",
    failed: zh ? "一份作答未获得有效结果" : "An answer did not produce a valid result",
    cancelled: zh ? "处理已停止" : "Processing stopped",
    started: zh ? "开始处理本批作答" : "Started processing this batch",
    completed: zh ? "本批结果已保存" : "Batch results saved",
    stopped: zh ? "本次处理已停止" : "This run has stopped",
  };
  const reasons = { rate_limit: zh ? "请求受限" : "Rate limited", timeout: zh ? "响应超时" : "Response timed out", unavailable: zh ? "请求暂未成功" : "Request temporarily unsuccessful" };
  const retry = event.code === "retrying" ? ` · ${event.reason ? reasons[event.reason] : ""}${event.attempt && event.max_attempts ? ` (${event.attempt}/${event.max_attempts})` : ""}` : "";
  return `${label ? `${label} · ` : ""}${labels[event.code]}${retry}`;
}

/** The parent keys this component by run: a new run gets a new history/cursor. */
export function GradingActivity({ taskId, progress, locale, stopped = false }: {
  taskId: string; progress: JobProgress; locale: Locale; stopped?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [events, setEvents] = useState<GradingActivityEvent[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  const cursor = useRef(0);
  const stickToBottom = useRef(true);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();
  const zh = locale === "zh-CN";
  const runId = progress.job_id;
  const target = progress.activity_cursor ?? 0;
  const targetRef = useRef(target);
  targetRef.current = target;
  const models = progress.model_progress ?? [];

  useEffect(() => {
    if (!expanded || !runId) return;
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setError(false);
    async function load() {
      if (cursor.current > 0 && cursor.current >= targetRef.current) {
        timer = setTimeout(() => void load(), 1500);
        return;
      }
      setLoading(true);
      try {
        do {
          const page = await getGradingActivity(taskId, runId!, cursor.current, controller.signal);
          if (!active || page.run_id !== runId) return;
          const next = page.events.filter((event) => event.sequence > cursor.current);
          setEvents((previous) => [...previous, ...next]);
          const advanced = page.cursor > cursor.current;
          cursor.current = page.cursor;
          if (!advanced || !page.has_more || cursor.current >= targetRef.current) break;
        } while (active);
        if (active) timer = setTimeout(() => void load(), 1500);
      } catch {
        if (active) setError(true);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [expanded, runId, taskId, retry]);

  useEffect(() => {
    if (expanded && stickToBottom.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [expanded, events]);

  const latestText = stopped ? (zh ? "本次处理已停止" : "This run has stopped") : gradingActivityText(progress.activity_latest, models, zh);
  return <section className="mt-3 overflow-hidden rounded-lg border bg-card text-xs text-muted-foreground">
    <button type="button" className="flex h-10 w-full min-w-0 items-center gap-2 px-4 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      aria-expanded={expanded} aria-controls={id} onClick={() => { stickToBottom.current = true; setExpanded(!expanded); }}>
      <span className="shrink-0 font-medium">{zh ? "最近动态" : "Recent activity"}</span>
      <span className="truncate" aria-live="polite">{latestText}</span>
      <ChevronDown aria-hidden="true" className={`ml-auto h-3.5 w-3.5 shrink-0 ${expanded ? "rotate-180" : ""}`} />
    </button>
    {expanded && <div id={id} ref={list} className="h-56 overflow-y-auto border-t px-4 py-2" role="region" aria-label={zh ? "批改动态历史" : "Grading activity history"} tabIndex={0}
      onScroll={() => { if (list.current) stickToBottom.current = list.current.scrollHeight - list.current.scrollTop - list.current.clientHeight < 32; }}>
      <ol className="space-y-2">
        {events.map((event) => <li key={event.sequence} className="flex gap-3 leading-5">
          <time className="shrink-0 tabular-nums" dateTime={new Date(event.ts * 1000).toISOString()}>{new Date(event.ts * 1000).toLocaleTimeString(locale)}</time>
          <span className="min-w-0 break-words">{gradingActivityText(event, models, zh)}</span>
        </li>)}
      </ol>
      {loading && <p className="py-2">{zh ? "读取动态…" : "Loading activity…"}</p>}
      {error && <button type="button" className="py-2 text-primary underline" onClick={() => setRetry((value) => value + 1)}>{zh ? "动态读取失败，点击重试" : "Activity could not load. Retry"}</button>}
      {!loading && !error && !events.length && <p className="py-2">{zh ? "暂无动态记录" : "No activity recorded yet"}</p>}
    </div>}
  </section>;
}

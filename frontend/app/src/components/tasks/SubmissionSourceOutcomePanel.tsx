import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Loader2,
  Upload,
  UserRoundSearch,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { getSubmissionSourceReasonCopy } from "@/lib/submissionSourceOutcomes";
import type { SubmissionSourceOutcome, SubmissionSourceSummary } from "@/types";

export function SubmissionSourceOutcomePanel({
  summary,
  sources = [],
  locale,
  taskId,
  className,
}: {
  summary?: SubmissionSourceSummary;
  sources?: SubmissionSourceOutcome[];
  locale: Locale;
  taskId?: string;
  className?: string;
}) {
  const counts = summary ?? summarize(sources);
  if (counts.uploaded <= 0) return null;
  const hasTerminalAttention = counts.failed > 0 || counts.identity_needs_review > 0;
  const hasQuestionWarnings = sources.some((source) => source.unknown_question_ids.length > 0);
  const hasAttention = hasTerminalAttention || hasQuestionWarnings;
  const ordered = [...sources].sort((left, right) => {
    const priority = (item: SubmissionSourceOutcome) => (
      item.status === "failed" ? 0
        : item.status === "identity_needs_review" ? 1
          : item.status === "processing" ? 2 : 3
    );
    return priority(left) - priority(right) || left.file_name.localeCompare(right.file_name);
  });

  return (
    <section
      aria-labelledby="submission-source-outcomes-title"
      className={cn("rounded-[10px] border bg-card px-4 py-4 sm:px-5", className)}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 id="submission-source-outcomes-title" className="text-sm font-bold text-foreground">
            {tx(locale, "本批文件识别结果", "Submission source results")}
          </h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {equation(counts, locale)}
            {hasTerminalAttention
              ? tx(locale, "。失败文件不会被静默丢弃，待确认身份也不会覆盖其他学生。", ". Failed files are not silently dropped, and unresolved identities do not overwrite other students.")
              : hasQuestionWarnings
                ? tx(locale, "。部分成功文件还包含未匹配题号，请展开核对。", ". Some recognized files also contain unmatched question IDs; expand the list to review them.")
              : tx(locale, "。每一份原文件都有明确结果。", ". Every original file has an explicit result.")}
          </p>
        </div>
        {taskId && hasAttention ? (
          <Link
            to={`/tasks/${taskId}/submissions/upload`}
            className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border bg-card px-3 text-xs font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Upload aria-hidden="true" className="h-3.5 w-3.5" />
            {tx(locale, "重新选择文件", "Choose files again")}
          </Link>
        ) : null}
      </div>

      <div className="mt-3 flex flex-wrap gap-2" aria-label={tx(locale, "识别结果统计", "Recognition totals")}>
        <CountBadge tone="success" label={tx(locale, "成功", "Parsed")} value={counts.parsed} />
        <CountBadge tone="danger" label={tx(locale, "失败", "Failed")} value={counts.failed} />
        <CountBadge tone="warning" label={tx(locale, "身份待确认", "Identity review")} value={counts.identity_needs_review} />
        {counts.pending > 0 ? (
          <CountBadge tone="neutral" label={tx(locale, "处理中", "Processing")} value={counts.pending} />
        ) : null}
      </div>

      {ordered.length > 0 ? (
        <details className="group mt-4" open={hasAttention}>
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-md bg-slate-50 px-3 py-2 text-xs font-semibold text-foreground outline-none hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-ring dark:bg-slate-900/50 dark:hover:bg-slate-900 [&::-webkit-details-marker]:hidden">
            <span>
              {hasAttention
                ? tx(locale, "查看每份文件及具体原因", "Review every file and its exact reason")
                : tx(locale, "查看每份文件", "Review every file")}
            </span>
            <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180" />
          </summary>
          <ol className="mt-2 max-h-[520px] space-y-2 overflow-y-auto pr-1">
            {ordered.map((source) => (
              <SourceOutcomeRow key={source.source_id} source={source} locale={locale} />
            ))}
          </ol>
        </details>
      ) : null}
    </section>
  );
}

function SourceOutcomeRow({ source, locale }: { source: SubmissionSourceOutcome; locale: Locale }) {
  const copy = getSubmissionSourceReasonCopy(source, locale);
  const Icon = source.status === "parsed"
    ? CheckCircle2
    : source.status === "identity_needs_review"
      ? UserRoundSearch
      : source.status === "processing"
        ? Loader2
        : AlertTriangle;
  const tone = source.status === "parsed"
    ? "text-emerald-700 bg-emerald-50 dark:bg-emerald-950/40 dark:text-emerald-200"
    : source.status === "identity_needs_review"
      ? "text-amber-700 bg-amber-50 dark:bg-amber-950/40 dark:text-amber-200"
      : source.status === "processing"
        ? "text-muted-foreground bg-slate-100 dark:bg-slate-800"
        : "text-red-700 bg-red-50 dark:bg-red-950/40 dark:text-red-200";

  return (
    <li className="rounded-lg border px-3 py-3">
      <div className="flex items-start gap-3">
        <span className={cn("mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full", tone)}>
          <Icon aria-hidden="true" className={cn("h-4 w-4", source.status === "processing" && "animate-spin")} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h3 className="min-w-0 break-all text-xs font-bold text-foreground">{source.file_name}</h3>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              {source.content_type} · {formatBytes(source.size_bytes, locale)}
            </span>
          </div>
          <p className="mt-1 text-xs font-semibold text-foreground">{copy.title}</p>
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground">{copy.description}</p>
          {copy.nextStep ? (
            <p className="mt-1 text-[11px] font-medium leading-5 text-foreground">
              {tx(locale, "建议：", "Next: ")}{copy.nextStep}
            </p>
          ) : null}
          {source.unknown_question_ids.length > 0 && source.reason_code !== "no_matching_answer" ? (
            <p className="mt-1 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] font-medium leading-5 text-amber-800 dark:bg-amber-950/35 dark:text-amber-200">
              {tx(locale, "同时发现未匹配题号：", "Also found unmatched question IDs: ")}
              {source.unknown_question_ids.join(locale === "zh-CN" ? "、" : ", ")}
            </p>
          ) : null}
          {source.reason_code || source.failure_phase ? (
            <details className="mt-2">
              <summary className="w-fit cursor-pointer text-[10px] font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                {tx(locale, "诊断信息", "Diagnostic details")}
              </summary>
              <dl className="mt-1.5 grid gap-x-4 gap-y-1 rounded-md bg-slate-50 px-2.5 py-2 text-[10px] dark:bg-slate-950/40 sm:grid-cols-2">
                <TechnicalRow label={tx(locale, "错误代码", "Error code")} value={source.reason_code} />
                <TechnicalRow label={tx(locale, "原识别代码", "Recognition code")} value={source.recognition_reason_code && source.recognition_reason_code !== source.reason_code ? source.recognition_reason_code : null} />
                <TechnicalRow label={tx(locale, "处理结果", "Resolution")} value={source.resolution_status === "identity_resolved" ? tx(locale, "教师已确认身份", "Identity confirmed by teacher") : null} />
                <TechnicalRow label={tx(locale, "失败阶段", "Failure phase")} value={source.failure_phase} />
                <TechnicalRow label={tx(locale, "任务编号", "Job ID")} value={source.job_id} />
                <TechnicalRow label={tx(locale, "来源编号", "Source ID")} value={source.source_id} />
                <TechnicalRow label={tx(locale, "追踪编号", "Trace ID")} value={source.trace_id} />
                <TechnicalRow label={tx(locale, "可直接重试", "Retryable")} value={source.retryable ? tx(locale, "是", "Yes") : tx(locale, "否", "No")} />
              </dl>
            </details>
          ) : null}
        </div>
      </div>
    </li>
  );
}

function TechnicalRow({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="grid grid-cols-[max-content_minmax(0,1fr)] gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-foreground">{value}</dd>
    </div>
  );
}

function CountBadge({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "success" | "danger" | "warning" | "neutral";
}) {
  return (
    <span className={cn(
      "inline-flex min-h-7 items-center gap-1.5 rounded-full px-2.5 text-[11px] font-semibold",
      tone === "success" && "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-200",
      tone === "danger" && "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-200",
      tone === "warning" && "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-200",
      tone === "neutral" && "bg-slate-100 text-muted-foreground dark:bg-slate-800",
    )}>
      {label}<strong className="font-mono text-xs">{value}</strong>
    </span>
  );
}

function summarize(sources: SubmissionSourceOutcome[]): SubmissionSourceSummary {
  return {
    uploaded: sources.length,
    parsed: sources.filter((item) => item.status === "parsed").length,
    failed: sources.filter((item) => item.status === "failed").length,
    identity_needs_review: sources.filter((item) => item.status === "identity_needs_review").length,
    pending: sources.filter((item) => item.status === "processing").length,
  };
}

function equation(summary: SubmissionSourceSummary, locale: Locale): string {
  const base = locale === "en-US"
    ? `${summary.uploaded} uploaded = ${summary.parsed} parsed + ${summary.failed} failed + ${summary.identity_needs_review} identity review`
    : `${summary.uploaded} 份上传 = ${summary.parsed} 份成功 + ${summary.failed} 份失败 + ${summary.identity_needs_review} 份身份待确认`;
  if (!summary.pending) return base;
  return locale === "en-US" ? `${base} + ${summary.pending} processing` : `${base} + ${summary.pending} 份处理中`;
}

function formatBytes(value: number, locale: Locale): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: value >= 1024 * 1024 ? "megabyte" : value >= 1024 ? "kilobyte" : "byte",
    unitDisplay: "short",
    maximumFractionDigits: 1,
  }).format(value >= 1024 * 1024 ? value / (1024 * 1024) : value >= 1024 ? value / 1024 : value);
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

import {
  Archive,
  CheckCircle2,
  Download,
  FileJson,
  FileSpreadsheet,
  FileText,
  History,
  LoaderCircle,
  PackageCheck,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getTaskResultArtifactBlob } from "@/api/tasks";
import { useDeleteTaskResultArtifact, useGenerateTaskResultArtifacts, useTaskResultArtifacts } from "@/api/hooks/tasks";
import { normalizeAPIError } from "@/api/client";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { classifyRecoverableError, isCurrentResultArtifactReady } from "@/lib/taskActionGuards";
import { formatTaskTime } from "@/lib/taskFlow";
import type { ResultArtifactFile, ResultArtifactVersion, TaskFinalizationResponse } from "@/types";
import { toast } from "sonner";

const FILE_DETAILS: Record<ResultArtifactFile["artifact_id"], { description: string; descriptionEn: string; extension: string }> = {
  grades_csv: { description: "教师最终分、总分率与逐题得分，可直接用表格软件打开。", descriptionEn: "Teacher-final scores, totals, and per-question scores for spreadsheet use.", extension: "CSV" },
  learning_report_md: { description: "班级、逐题与学生事实汇总；不含 AI 自动推断结论。", descriptionEn: "Factual class, question, and student summary without AI-inferred claims.", extension: "MD" },
  published_answers_md: { description: "题干、评分标准和参考答案的可编辑发布稿。", descriptionEn: "Editable release containing questions, rubrics, and reference answers.", extension: "MD" },
  published_answers_tex: { description: "同一发布版标答的可编译 LaTeX 源文件。", descriptionEn: "Compilable LaTeX source for the same published reference answers.", extension: "TEX" },
  formal_result_json: { description: "含版本、指纹与完整正式结果的机器可读快照。", descriptionEn: "Machine-readable snapshot containing the version, fingerprint, and complete final results.", extension: "JSON" },
};

export function ReportsDownloadsPage({
  locale,
  taskId,
  taskName,
  finalization,
}: {
  locale: Locale;
  taskId: string;
  taskName: string;
  finalization: TaskFinalizationResponse;
}) {
  const artifactsQuery = useTaskResultArtifacts(taskId);
  const generateMutation = useGenerateTaskResultArtifacts();
  const deleteMutation = useDeleteTaskResultArtifact();
  const [downloading, setDownloading] = useState<string | null>(null);
  const [generateFailure, setGenerateFailure] = useState<unknown>(null);
  // JSON exports are paused, including entries from cached legacy manifests.
  const index = artifactsQuery.data ? {
    ...artifactsQuery.data,
    versions: artifactsQuery.data.versions.map((version) => ({
      ...version,
      files: version.files.filter((file) => file.artifact_id !== "formal_result_json"),
    })),
  } : undefined;
  const storedCurrent = index?.versions.find((version) => version.current);
  const stale = finalization.final_result_dirty || storedCurrent?.status === "stale";
  const current: ResultArtifactVersion | undefined = (stale ? undefined : storedCurrent)
    ?? (index
      ? { version: 1, current: true, status: "not_generated", files: [] } : undefined);
  const historical = index?.versions.filter((version) => version.files.length > 0 && (!version.current || stale)) ?? [];
  const ready = isCurrentResultArtifactReady({
    finalResultDirty: finalization.final_result_dirty,
    status: current?.status,
    fileCount: current?.files.length ?? 0,
  });
  const generating = generateMutation.isPending;

  const generate = async () => {
    setGenerateFailure(null);
    try {
      const result = await generateMutation.mutateAsync({ taskId, expectedWorkflowRevision: finalization.workflow_revision });
      toast.success(result.unchanged ? tx(locale, "当前版本已是最新", "Current version is already up to date") : tx(locale, "本版报告已生成", "Reports generated for this version"));
    } catch (error) {
      setGenerateFailure(error);
    }
  };

  const download = async (version: number, artifactId: string, filename: string) => {
    const key = `${version}:${artifactId}`;
    setDownloading(key);
    try {
      const blob = await getTaskResultArtifactBlob(taskId, version, artifactId);
      triggerDownload(blob, filename);
    } catch (error) {
      toast.error(tx(locale, "下载失败", "Download failed"), { description: normalizeAPIError(error).message });
    } finally {
      setDownloading(null);
    }
  };

  const remove = async (version: number) => {
    if (!window.confirm(tx(locale, `删除历史报告 v${version}？批改分数和复核记录会保留。`, `Delete historical report v${version}? Scores and review records will be kept.`))) return;
    try {
      await deleteMutation.mutateAsync({ taskId, version });
    } catch (error) {
      toast.error(tx(locale, "删除失败", "Delete failed"), { description: normalizeAPIError(error).message });
    }
  };

  if (artifactsQuery.isLoading) {
    return <section className="flex min-h-72 items-center justify-center rounded-[10px] border bg-card"><LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" /></section>;
  }
  if (artifactsQuery.isError || !index || !current) {
    const info = classifyRecoverableError(
      artifactsQuery.error ?? "formal_result_version_not_found",
      { locale, phase: "result_artifact_index", returnTo: `/tasks/${taskId}/results/reports` },
    );
    return (
      <RecoverableActionState
        info={info}
        locale={locale}
        className="min-h-72"
        primaryAction={info.actionKind === "byok" ? undefined : {
          label: tx(locale, "重新读取", "Try again"),
          onClick: () => void artifactsQuery.refetch(),
          busy: artifactsQuery.isFetching,
        }}
        secondaryAction={{ label: tx(locale, "返回结果总览", "Back to overview"), href: `/tasks/${taskId}/results` }}
      />
    );
  }

  const generateRecovery = generateFailure
    ? classifyRecoverableError(generateFailure, {
      locale,
      phase: "result_artifact_generation",
      returnTo: `/tasks/${taskId}/results/reports`,
    })
    : null;

  return (
    <section className="rounded-[10px] border bg-card">
      <div className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">{tx(locale, "最新报告", "Latest report")}</h2><p className="mt-1 text-[13px] text-muted-foreground">{tx(locale, "根据当前已保存的数据生成报告。", "Generate a report from the currently saved data.")}</p></div>
          {ready ? <button type="button" disabled={downloading !== null} onClick={() => void download(current.version, "bundle", current.bundle_filename ?? "reports.zip")} className="inline-flex h-9 items-center gap-1.5 rounded-[8px] bg-primary px-3.5 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{downloading === `${current.version}:bundle` ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Archive aria-hidden="true" className="h-4 w-4" />}{tx(locale, "下载全部 ZIP", "Download all ZIP")}</button> : null}
        </div>

        {ready ? <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
          <Metric label={tx(locale, "报告版本", "Report version")} value={`v${current.version}`} tone="primary" />
          <Metric label={tx(locale, "文件状态", "File status")} value={tx(locale, "可下载", "Ready")} tone="accent" />
          <Metric label={tx(locale, "可下载文件", "Downloadable files")} value={String(current.files.length)} tone="primary" />
          <Metric label={tx(locale, "生成时间", "Generated at")} value={current.generated_at ? formatTaskTime(current.generated_at, true, locale) : "—"} tone="neutral" compact />
        </div> : null}

        {!ready ? (
          <div className="mt-4 flex flex-wrap items-center gap-3 rounded-[9px] border border-blue-100 bg-blue-50/60 px-4 py-4">
            <span className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-blue-100 text-primary"><PackageCheck aria-hidden="true" className="h-[18px] w-[18px]" /></span>
            <div className="min-w-0 flex-1"><p className="text-[13px] font-bold text-foreground">{tx(locale, "最新报告尚未生成，请先生成再下载。", "No latest report yet. Generate it before downloading.")}</p><p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{tx(locale, "包含成绩表、学情报告和参考答案。", "Includes grades, a performance report, and reference answers.")}</p></div>
            {!finalization.ready_for_confirmation ? <Link to={`/tasks/${encodeURIComponent(taskId)}/review`} className="text-[11px] font-semibold text-primary hover:underline">{tx(locale, "返回复核并确认", "Return to review")}</Link> : <button type="button" onClick={generate} disabled={generating} className="inline-flex h-9 items-center gap-1.5 rounded-[8px] bg-primary px-3.5 text-[11px] font-semibold text-primary-foreground disabled:opacity-50">{generating ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <RefreshCw aria-hidden="true" className="h-4 w-4" />}{generating ? tx(locale, "生成中…", "Generating…") : tx(locale, "生成本版文件", "Generate files")}</button>}
          </div>
        ) : (
          <div className="mt-4 flex items-center gap-2 rounded-[9px] bg-emerald-50 px-4 py-3 text-[11px] text-emerald-800"><CheckCircle2 aria-hidden="true" className="h-4 w-4" /><span className="font-semibold">{taskName} · v{current.version}</span><span>{tx(locale, "报告已生成，可以下载。", "Report generated and ready to download.")}</span></div>
        )}
        {generateRecovery ? (
          <RecoverableActionState
            info={generateRecovery}
            locale={locale}
            compact
            className="mt-4"
            primaryAction={generateRecovery.actionKind === "byok" ? undefined : {
              label: tx(locale, "重新生成", "Generate again"),
              onClick: generate,
              busy: generating,
            }}
            secondaryAction={{ label: tx(locale, "关闭提示", "Dismiss"), onClick: () => setGenerateFailure(null) }}
          />
        ) : null}
      </div>

      {ready ? <div className="border-t">
        <div className="grid min-h-10 grid-cols-[minmax(0,1fr)_90px_92px] items-center gap-3 bg-slate-50 px-5 text-[10px] font-semibold text-muted-foreground sm:grid-cols-[minmax(0,1fr)_100px_120px_92px]"><span>{tx(locale, "文件", "File")}</span><span className="hidden sm:block">{tx(locale, "大小", "Size")}</span><span>{tx(locale, "版本状态", "Version")}</span><span className="text-right">{tx(locale, "操作", "Action")}</span></div>
        {current.files.map((file) => <ArtifactRow key={file.artifact_id} locale={locale} file={file} version={current.version} downloading={downloading === `${current.version}:${file.artifact_id}`} onDownload={() => void download(current.version, file.artifact_id, file.filename)} />)}
      </div> : null}

      <div className="border-t px-5 py-4">
        <h2 className="flex items-center justify-between gap-3 text-[16px] font-bold text-foreground"><span className="inline-flex items-center gap-2"><History aria-hidden="true" className="h-4 w-4 text-muted-foreground" />{tx(locale, "历史报告", "Historical reports")}</span><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-muted-foreground">{historical.length}</span></h2>
        <div className="mt-3 grid gap-2">{historical.length ? historical.map((version) => <HistoricalVersion key={version.version} locale={locale} version={version} downloading={downloading === `${version.version}:bundle`} deleting={deleteMutation.isPending} onDownload={() => void download(version.version, "bundle", version.bundle_filename ?? "reports.zip")} onDelete={() => void remove(version.version)} />) : <p className="rounded-[8px] bg-muted/50 px-3 py-4 text-center text-[11px] text-muted-foreground">{tx(locale, "暂无历史报告。", "No historical reports.")}</p>}</div>
      </div>
    </section>
  );
}

function ArtifactRow({ locale, file, version, downloading, onDownload }: { locale: Locale; file: ResultArtifactFile; version: number; downloading: boolean; onDownload: () => void }) {
  const detail = FILE_DETAILS[file.artifact_id];
  const Icon = file.artifact_id === "grades_csv" ? FileSpreadsheet : file.artifact_id === "formal_result_json" ? FileJson : FileText;
  return <div className="grid min-h-[68px] grid-cols-[minmax(0,1fr)_90px_92px] items-center gap-3 border-t px-5 py-3 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_100px_120px_92px]"><div className="flex min-w-0 items-center gap-3"><span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] bg-blue-50 text-primary"><Icon aria-hidden="true" className="h-[17px] w-[17px]" /></span><div className="min-w-0"><p className="truncate text-[12px] font-bold text-foreground">{locale === "en-US" ? file.title_en : file.title}<span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-semibold text-muted-foreground">{detail.extension}</span></p><p className="mt-0.5 truncate text-[10px] text-muted-foreground">{locale === "en-US" ? detail.descriptionEn : detail.description}</p></div></div><span className="hidden text-[10px] text-muted-foreground sm:block">{formatBytes(file.size_bytes)}</span><span className="inline-flex w-fit rounded-full bg-emerald-50 px-2 py-1 text-[9px] font-semibold text-emerald-700">v{version} · {tx(locale, "当前", "Current")}</span><button type="button" disabled={downloading} onClick={onDownload} className="ml-auto inline-flex h-8 items-center gap-1 rounded-[7px] border px-2.5 text-[10px] font-semibold text-foreground hover:bg-muted disabled:opacity-50">{downloading ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Download aria-hidden="true" className="h-3.5 w-3.5" />}{tx(locale, "下载", "Download")}</button></div>;
}

function HistoricalVersion({ locale, version, downloading, deleting, onDownload, onDelete }: { locale: Locale; version: ResultArtifactVersion; downloading: boolean; deleting: boolean; onDownload: () => void; onDelete: () => void }) {
  return <div className="flex flex-wrap items-center gap-3 rounded-[8px] border px-3 py-3"><span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-bold text-muted-foreground">v{version.version}</span><div className="min-w-0 flex-1"><p className="text-[11px] font-semibold text-foreground">{tx(locale, `${version.files.length} 个文件可下载`, `${version.files.length} files available`)}</p><p className="mt-0.5 text-[10px] text-muted-foreground">{tx(locale, "生成时间", "Generated at")} · {version.generated_at ? formatTaskTime(version.generated_at, true, locale) : "—"}</p></div><button type="button" disabled={downloading || deleting} onClick={onDownload} className="inline-flex h-8 items-center gap-1 rounded-[7px] border px-2.5 text-[10px] font-semibold disabled:opacity-40">{downloading ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Archive aria-hidden="true" className="h-3.5 w-3.5" />}{tx(locale, "下载 ZIP", "Download ZIP")}</button><button type="button" disabled={deleting || downloading} onClick={onDelete} className="inline-flex h-8 items-center gap-1 rounded-[7px] border px-2.5 text-[10px] font-semibold text-destructive disabled:opacity-40"><Trash2 aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "删除", "Delete")}</button></div>;
}

function Metric({ label, value, tone, compact = false }: { label: string; value: string; tone: "primary" | "accent" | "warning" | "neutral"; compact?: boolean }) {
  return <div className="min-w-0 rounded-[9px] border px-3 py-3"><strong className={cn("block", compact ? "text-[12px] leading-6" : "truncate text-[18px] leading-6", tone === "primary" && "text-primary", tone === "accent" && "text-teal-600", tone === "warning" && "text-amber-600", tone === "neutral" && "text-foreground")}>{value}</strong><span className="mt-1 block text-[10px] font-medium text-muted-foreground">{label}</span></div>;
}

function formatBytes(value: number): string { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`; }
function triggerDownload(blob: Blob, filename: string) { const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }
function tx(locale: Locale, zh: string, en: string): string { return locale === "en-US" ? en : zh; }

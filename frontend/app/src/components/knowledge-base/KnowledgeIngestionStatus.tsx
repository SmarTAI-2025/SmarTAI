import { useEffect, useRef, useState } from "react";
import { ChevronRight, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";
import { getJSON, postJSON } from "@/api/client";
import type { KnowledgeIngestionSummary } from "@/types/personalKnowledge";
import { backgroundErrorTitle } from "@/lib/taskActionGuards";

interface Coverage {
  summary: KnowledgeIngestionSummary;
  pages: { page_number: number; state: string; error_code?: string; warning_codes?: string[] }[];
  next_offset: number | null;
}

function pageStateLabel(state: string, zh: boolean) {
  const labels: Record<string, [string, string]> = {
    unprocessed: ["待处理", "Pending"], processing: ["处理中", "Processing"],
    searchable: ["可检索", "Searchable"], searchable_with_warning: ["可检索，待核对", "Searchable, unverified"],
    blank_confirmed: ["已确认空白", "Confirmed blank"], failed: ["内容不完整", "Incomplete"],
  };
  return labels[state]?.[zh ? 0 : 1] ?? (zh ? "状态待确认" : "Unknown status");
}

export function KnowledgeIngestionStatus({ documentId, status = "ready", ingestion, zh, poll = true, onChange, detailsLabel }: {
  documentId?: string; status?: string; ingestion?: KnowledgeIngestionSummary; zh: boolean;
  poll?: boolean; onChange?: () => void; detailsLabel?: string;
}) {
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const identity = useRef(documentId);
  identity.current = documentId;
  useEffect(() => { setCoverage(null); setError(false); }, [documentId, ingestion?.id, ingestion?.processed_pages, ingestion?.status]);
  const summary = coverage?.summary ?? ingestion;
  const active = ["queued", "processing"].includes(summary?.status ?? "");
  const paused = ["paused", "cancelled"].includes(summary?.status ?? "");
  useEffect(() => {
    if (!poll || !active || !documentId) return;
    let cancelled = false;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await getJSON<Coverage>(`/knowledge/documents/${documentId}/coverage?offset=0&limit=20`);
        if (!cancelled && identity.current === documentId) { setCoverage(result); setError(false); }
      } catch { if (!cancelled) setError(true); }
      finally { pending = false; }
    }, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active, documentId, summary?.id, poll]);
  const effectiveStatus = summary?.status ?? status;
  const label = paused ? (zh ? "已暂停" : "Paused") : active ? (zh ? "后台处理中" : "Processing")
    : effectiveStatus === "partial" ? (zh ? "部分可检索" : "Partial coverage")
    : effectiveStatus === "failed" ? (zh ? "处理未完成" : "Incomplete")
    : summary?.warning_pages ? (zh ? "可检索，有待核对" : "Searchable, with warnings") : (zh ? "已解析" : "Parsed");

  async function load(offset = 0) {
    if (!documentId) return;
    setBusy(true); setError(false);
    try {
      const result = await getJSON<Coverage>(`/knowledge/documents/${documentId}/coverage?offset=${offset}&limit=20`);
      if (identity.current === documentId) setCoverage(result);
    } catch { if (identity.current === documentId) setError(true); }
    finally { if (identity.current === documentId) setBusy(false); }
  }
  async function command(action: "resume" | "cancel" | "retry-failed") {
    if (!documentId) return;
    setBusy(true); setError(false);
    try { await postJSON(`/knowledge/documents/${documentId}/${action}`, {}); await load(); onChange?.(); }
    catch { setError(true); }
    finally { setBusy(false); }
  }
  if (!documentId || !summary?.id) return <span className="text-xs text-muted-foreground">{label}</span>;
  return <details className="min-w-0 text-xs" onToggle={(event) => { if (event.currentTarget.open && !coverage && !busy) void load(); }}>
    <summary className="cursor-pointer break-words text-muted-foreground">{detailsLabel ?? label}
      {!detailsLabel && summary.total_pages ? ` · ${summary.processed_pages ?? 0}/${summary.total_pages}` : ""}
    </summary>
    <div className="mt-2 max-w-xs space-y-2 break-words">
      {summary.total_pages ? <p>{zh ? "可检索 / 空白 / 未完整识别" : "Searchable / blank / incomplete"}: {summary.searchable_pages ?? 0} / {summary.blank_pages ?? 0} / {summary.failed_pages ?? 0}</p> : null}
      {summary.partially_searchable_pages ? <p>{zh ? "可检索页中仍有内容缺口" : "Searchable pages with content gaps"}: {summary.partially_searchable_pages}</p> : null}
      {summary.error_code ? <p role="status">{backgroundErrorTitle(summary.error_code, zh ? "zh-CN" : "en-US")}</p> : null}
      {coverage?.pages.map((page) => <p key={page.page_number}>{page.page_number}: {pageStateLabel(page.state, zh)}{page.error_code ? ` · ${backgroundErrorTitle(page.error_code, zh ? "zh-CN" : "en-US")}` : ""}</p>)}
      {error ? <p role="alert">{zh ? "暂时无法更新状态" : "Unable to update status"}</p> : null}
      <div className="flex gap-2">
        <button type="button" disabled={busy} onClick={() => void load()} title={zh ? "刷新状态" : "Refresh status"} aria-label={zh ? "刷新状态" : "Refresh status"}><RefreshCw className="h-4 w-4" /></button>
        {active || paused ? <button type="button" disabled={busy} onClick={() => void command(paused ? "resume" : "cancel")} title={paused ? (zh ? "继续处理" : "Resume") : (zh ? "暂停处理" : "Pause")} aria-label={paused ? (zh ? "继续处理" : "Resume") : (zh ? "暂停处理" : "Pause")}>{paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}</button> : null}
        {!active && (summary.failed_pages || paused) ? <button type="button" disabled={busy} onClick={() => void command("retry-failed")} title={zh ? "用当前默认模型重试缺页" : "Retry gaps with current default model"} aria-label={zh ? "用当前默认模型重试缺页" : "Retry gaps with current default model"}><RotateCcw className="h-4 w-4" /></button> : null}
        {coverage?.next_offset != null ? <button type="button" disabled={busy} onClick={() => void load(coverage.next_offset!)} title={zh ? "下一批页" : "Next pages"} aria-label={zh ? "下一批页" : "Next pages"}><ChevronRight className="h-4 w-4" /></button> : null}
      </div>
    </div>
  </details>;
}

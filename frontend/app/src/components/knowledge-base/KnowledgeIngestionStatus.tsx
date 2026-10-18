import { useEffect, useRef, useState } from "react";
import { ChevronRight, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";
import { getJSON, postJSON, postMultipart } from "@/api/client";
import type { KnowledgeIngestionSummary } from "@/types/personalKnowledge";
import { backgroundErrorTitle } from "@/lib/taskActionGuards";
import { useFrontierWorkspace } from "@/lib/frontierContext";
import { listStageProviders } from "@/api/experts";
import { modelDisplayName, sortByProvider } from "@/lib/modelPresentation";
import type { ExpertConfig } from "@/types";

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
  const frontier = useFrontierWorkspace();
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [resubmitUncertain, setResubmitUncertain] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [experts, setExperts] = useState<ExpertConfig[]>([]);
  const [modelError, setModelError] = useState(false);
  const [routeId, setRouteId] = useState("");
  const identity = useRef(documentId);
  identity.current = documentId;
  useEffect(() => { setCoverage(null); setError(false); }, [documentId, ingestion?.id, ingestion?.processed_pages, ingestion?.status]);
  useEffect(() => { setResubmitUncertain(false); }, [documentId, ingestion?.id]);
  useEffect(() => { setRouteId(""); }, [documentId]);
  const summary = coverage?.summary ?? ingestion;
  const active = ["queued", "processing"].includes(summary?.status ?? "");
  const paused = ["paused", "cancelled"].includes(summary?.status ?? "");
  const canRetry = !active && (!!summary?.failed_pages || paused);
  useEffect(() => {
    if (frontier || !expanded || !canRetry) return;
    let cancelled = false;
    setModelError(false);
    void listStageProviders().then((items) => {
      if (!cancelled) setExperts(sortByProvider(items.filter((item) => item.enabled)));
    }).catch(() => { if (!cancelled) setModelError(true); });
    return () => { cancelled = true; };
  }, [frontier, expanded, canRetry, documentId]);
  useEffect(() => {
    if (frontier || !poll || !active || !documentId) return;
    let cancelled = false;
    let pending = false;
    const timer = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await getJSON<Coverage>(`/knowledge/documents/${documentId}/coverage?offset=0&limit=20&issues_only=true`);
        if (!cancelled && identity.current === documentId) { setCoverage(result); setError(false); }
      } catch { if (!cancelled) setError(true); }
      finally { pending = false; }
    }, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [frontier, active, documentId, summary?.id, poll]);
  const effectiveStatus = summary?.status ?? status;
  const label = paused ? (zh ? "已暂停" : "Paused") : active ? (zh ? "后台处理中" : "Processing")
    : effectiveStatus === "partial" ? (zh ? "部分可检索" : "Partial coverage")
    : effectiveStatus === "failed" ? (zh ? "处理未完成" : "Incomplete")
    : summary?.warning_pages ? (zh ? "可检索，有待核对" : "Searchable, with warnings") : (zh ? "已解析" : "Parsed");

  async function load(offset = 0) {
    if (!documentId) return;
    setBusy(true); setError(false);
    try {
      const result = await getJSON<Coverage>(`/knowledge/documents/${documentId}/coverage?offset=${offset}&limit=20&issues_only=true`);
      if (identity.current === documentId) setCoverage(result);
    } catch { if (identity.current === documentId) setError(true); }
    finally { if (identity.current === documentId) setBusy(false); }
  }
  async function command(action: "resume" | "cancel" | "retry-failed") {
    if (!documentId) return;
    setBusy(true); setError(false);
    try {
      const path = `/knowledge/documents/${documentId}/${action}`;
      if (action === "retry-failed") {
        await postMultipart(path, null, { fields: { accept_uncertain_resubmission: resubmitUncertain,
          ...(routeId ? { recognition_route_id: routeId } : {}) } });
        setResubmitUncertain(false);
      } else await postJSON(path, {});
      await load(); onChange?.();
    }
    catch { setError(true); }
    finally { setBusy(false); }
  }
  if (frontier || !documentId || !summary?.id) return <span className="text-xs text-muted-foreground">{label}</span>;
  const hasIssues = active || paused || effectiveStatus === "partial" || effectiveStatus === "failed" || !!summary.failed_pages;
  if (!hasIssues) return <span className="text-xs text-muted-foreground">{label}{summary.total_pages ? ` · ${summary.processed_pages ?? 0}/${summary.total_pages}` : ""}</span>;
  return <details className="min-w-[180px] text-xs" onToggle={(event) => { setExpanded(event.currentTarget.open); if (event.currentTarget.open && !coverage && !busy) void load(); }}>
    <summary className="cursor-pointer break-words text-muted-foreground">{detailsLabel ?? label}
      {!detailsLabel && summary.total_pages ? ` · ${summary.processed_pages ?? 0}/${summary.total_pages}` : ""}
    </summary>
    <div className="mt-2 max-w-xs space-y-2 break-words">
      {summary.total_pages ? <p>{zh ? "可检索 / 空白 / 未完整识别" : "Searchable / blank / incomplete"}: {summary.searchable_pages ?? 0} / {summary.blank_pages ?? 0} / {summary.failed_pages ?? 0}</p> : null}
      {summary.partially_searchable_pages ? <p>{zh ? "可检索页中仍有内容缺口" : "Searchable pages with content gaps"}: {summary.partially_searchable_pages}</p> : null}
      {summary.error_code ? <p role="status">{backgroundErrorTitle(summary.error_code, zh ? "zh-CN" : "en-US")}</p> : null}
      <div className="max-h-60 space-y-2 overflow-y-auto">
        {coverage?.pages.filter((page) => !["searchable", "searchable_with_warning", "blank_confirmed"].includes(page.state)).map((page) => <p key={page.page_number}>{page.page_number}: {pageStateLabel(page.state, zh)}{page.error_code ? ` · ${backgroundErrorTitle(page.error_code, zh ? "zh-CN" : "en-US")}` : ""}</p>)}
      </div>
      {error ? <p role="alert">{zh ? "暂时无法更新状态" : "Unable to update status"}</p> : null}
      {canRetry ? <label className="block space-y-1">
        <span>{zh ? "重试模型" : "Retry model"}</span>
        <select className="h-8 w-full min-w-0 rounded-md border bg-background px-2 text-xs disabled:opacity-50" value={routeId} disabled={busy} onChange={(event) => setRouteId(event.target.value)}>
          <option value="">{zh ? "当前默认模型" : "Current default model"}</option>
          {experts.map((expert) => <option key={expert.provider_id} value={expert.provider_id}>{modelDisplayName(expert)}{expert.provider_kind === "ocr" ? " · OCR" : ""}</option>)}
        </select>
        <span className="block text-muted-foreground">{zh ? "仅处理缺页，保留已成功页面。" : "Only gaps are retried; successful pages are kept."}</span>
        {modelError ? <span role="alert">{zh ? "模型列表暂不可用，请重新展开。" : "Model list unavailable. Reopen to refresh."}</span> : null}
      </label> : null}
      {!active && !!summary.failed_pages ? <label className="flex items-start gap-2">
        <input type="checkbox" checked={resubmitUncertain} disabled={busy} onChange={(event) => setResubmitUncertain(event.target.checked)} />
        <span>{zh ? "同时重试请求状态不明的页面（可能重复计费）" : "Also retry pages with unknown request status (may incur duplicate charges)"}</span>
      </label> : null}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="inline-flex min-h-8 items-center justify-center gap-1.5 rounded-md border bg-card px-2 py-1 text-xs hover:bg-muted disabled:opacity-50" disabled={busy} onClick={() => void load()} title={zh ? "刷新状态" : "Refresh status"} aria-label={zh ? "刷新状态" : "Refresh status"}><RefreshCw aria-hidden="true" className="h-4 w-4 shrink-0" /><span>{zh ? "刷新状态" : "Refresh status"}</span></button>
        {active || paused ? <button type="button" className="inline-flex min-h-8 items-center justify-center gap-1.5 rounded-md border bg-card px-2 py-1 text-xs hover:bg-muted disabled:opacity-50" disabled={busy} onClick={() => void command(paused ? "resume" : "cancel")} title={paused ? (zh ? "继续处理" : "Resume") : (zh ? "暂停处理" : "Pause")} aria-label={paused ? (zh ? "继续处理" : "Resume") : (zh ? "暂停处理" : "Pause")}>{paused ? <Play aria-hidden="true" className="h-4 w-4 shrink-0" /> : <Pause aria-hidden="true" className="h-4 w-4 shrink-0" />}<span>{paused ? (zh ? "继续处理" : "Resume") : (zh ? "暂停处理" : "Pause")}</span></button> : null}
        {canRetry ? <button type="button" className="inline-flex min-h-8 items-center justify-center gap-1.5 rounded-md border bg-card px-2 py-1 text-xs hover:bg-muted disabled:opacity-50" disabled={busy} onClick={() => void command("retry-failed")} title={zh ? "重试缺页" : "Retry gaps"} aria-label={zh ? "重试缺页" : "Retry gaps"}><RotateCcw aria-hidden="true" className="h-4 w-4 shrink-0" /><span>{zh ? "重试缺页" : "Retry gaps"}</span></button> : null}
        {coverage?.next_offset != null ? <button type="button" className="inline-flex min-h-8 items-center justify-center gap-1.5 rounded-md border bg-card px-2 py-1 text-xs hover:bg-muted disabled:opacity-50" disabled={busy} onClick={() => void load(coverage.next_offset!)} title={zh ? "下一批页" : "Next pages"} aria-label={zh ? "下一批页" : "Next pages"}><ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0" /><span>{zh ? "下一批页" : "Next pages"}</span></button> : null}
      </div>
    </div>
  </details>;
}

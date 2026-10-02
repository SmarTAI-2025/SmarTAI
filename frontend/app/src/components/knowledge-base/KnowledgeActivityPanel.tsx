import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BookOpen, ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { getJSON } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { knowledgeProgress, knowledgeStatusLabel } from "@/lib/knowledgeProgress";
import { KnowledgeIngestionStatus } from "./KnowledgeIngestionStatus";
import type { KnowledgeActivityFilter, KnowledgeActivityResponse } from "@/types/personalKnowledge";

export function KnowledgeActivityPanel({ compact = false }: { compact?: boolean }) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [state, setState] = useState<KnowledgeActivityFilter>("all");
  const [page, setPage] = useState(1);
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);
  const activity = useQuery({
    queryKey: ["knowledge-activity", { query, state, page, compact }],
    queryFn: ({ signal }) => getJSON<KnowledgeActivityResponse>(`/knowledge/activity?${new URLSearchParams({
      q: query, state, page: String(page), page_size: compact ? "5" : "20", prioritize_active: String(compact),
    })}`, { signal }),
    staleTime: 3000,
    refetchInterval: (result) => result.state.data?.active_count ? 5000 : false,
    refetchIntervalInBackground: false,
  });
  const data = activity.data;
  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / (compact ? 5 : 20)));
  useEffect(() => { if (data && page > pages) setPage(pages); }, [data, page, pages]);
  const options: [KnowledgeActivityFilter, string][] = [["all", zh ? "全部" : "All"],
    ["active", zh ? "进行中" : "In progress"], ["attention", zh ? "需处理" : "Needs attention"],
    ["completed", zh ? "已完成" : "Completed"]];

  return <section aria-label={zh ? "教材识别" : "Textbook recognition"} className="min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="flex items-center gap-2 text-base font-semibold"><BookOpen className="h-4 w-4" />{zh ? "教材识别" : "Textbook recognition"}</h2>
      <div className="flex items-center gap-3 text-sm">
        {data ? <span className="text-muted-foreground">{zh ? `进行中 ${data.active_count}` : `${data.active_count} in progress`}</span> : null}
        <button type="button" onClick={() => void activity.refetch()} disabled={activity.isFetching}
          aria-label={zh ? "刷新教材进度" : "Refresh textbook progress"} title={zh ? "刷新教材进度" : "Refresh textbook progress"}
          className="inline-flex h-8 w-8 items-center justify-center rounded border hover:bg-muted disabled:opacity-50"><RefreshCw className="h-4 w-4" /></button>
        {compact ? <Link className="text-primary hover:underline" to="/history?view=knowledge">{zh ? "全部记录" : "All records"}</Link> : null}
      </div>
    </div>
    {!compact ? <div className="mt-4 flex flex-wrap items-center gap-3">
      <label className="flex min-w-0 flex-1 items-center gap-2 border-b px-2"><Search className="h-4 w-4 shrink-0 text-muted-foreground" />
        <input value={search} maxLength={128} onChange={event => setSearch(event.target.value)}
          aria-label={zh ? "搜索教材" : "Search textbooks"} placeholder={zh ? "教材名称或文件名" : "Textbook or file name"}
          className="h-9 w-full min-w-0 bg-transparent text-sm outline-none" /></label>
      <select aria-label={zh ? "识别状态" : "Recognition status"} value={state}
        onChange={event => { setState(event.target.value as KnowledgeActivityFilter); setPage(1); }}
        className="h-9 max-w-full rounded border bg-background px-2 text-sm">
        {options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
    </div> : null}
    {activity.isError ? <p role="alert" className="mt-3 text-sm text-destructive">{zh ? "教材进度暂时无法更新，请刷新重试。" : "Unable to update textbook progress. Please refresh."}</p> : null}
    {activity.isPending ? <p role="status" className="py-6 text-sm text-muted-foreground">{zh ? "正在读取教材进度…" : "Loading textbook progress…"}</p>
      : !data?.items.length && !activity.isError ? <p className="py-6 text-sm text-muted-foreground">{zh ? "暂无教材识别记录" : "No textbook recognition records"}</p> : null}
    <ul className="mt-3 divide-y border-y">
      {data?.items.map(doc => {
        const summary = doc.ingestion;
        const progress = knowledgeProgress(summary);
        const status = doc.activity_status;
        const active = status === "queued" || status === "processing";
        const unit = summary?.unit === "section" ? (zh ? "段" : "sections") : summary?.unit === "slide" ? (zh ? "张" : "slides") : (zh ? "页" : "pages");
        const oldVersion = Boolean(summary?.id && doc.content_version !== summary.id && doc.chunk_count > 0);
        return <li key={doc.id} className="grid min-w-0 gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(180px,1fr)] sm:gap-6">
          <div className="min-w-0">
            <p className="break-words text-sm font-medium [overflow-wrap:anywhere]">{doc.title || doc.original_name}</p>
            {doc.title && doc.title !== doc.original_name ? <p className="mt-1 break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">{doc.original_name}</p> : null}
            <p className="mt-1 text-xs text-muted-foreground">{zh ? "更新于 " : "Updated "}{new Date(doc.updated_at * 1000).toLocaleString(locale)}</p>
          </div>
          <div className="min-w-0 space-y-2">
            <p role="status" className={`break-words text-sm font-medium ${active ? "text-primary" : status === "failed" || status === "partial" || status === "complete_with_warning" ? "text-amber-700 dark:text-amber-400" : "text-foreground"}`}>
              {knowledgeStatusLabel(status, zh)}
            </p>
            {progress ? <>
              <div className="flex flex-wrap justify-between gap-x-3 text-xs text-muted-foreground"><span>{zh ? "已处理" : "Processed"} {progress.processed}/{progress.total} {unit}</span><span>{progress.percent}%</span></div>
              <div role="progressbar" aria-label={zh ? `${doc.title || doc.original_name} 已处理进度` : `${doc.title || doc.original_name} processed progress`}
                aria-valuenow={progress.processed} aria-valuemin={0} aria-valuemax={progress.total} className="h-1.5 w-full overflow-hidden rounded bg-muted">
                <div className={`h-full ${active ? "bg-primary" : status === "complete" ? "bg-emerald-600" : "bg-amber-500"}`} style={{ width: `${progress.percent}%` }} />
              </div>
              <p className="break-words text-xs text-muted-foreground">{zh ? "可检索" : "Searchable"} {summary?.searchable_pages ?? 0} · {zh ? "未完整识别" : "Incomplete"} {summary?.failed_pages ?? 0} · {zh ? "待核对" : "Warnings"} {(summary?.warning_pages ?? 0) + (summary?.partially_searchable_pages ?? 0)}{summary?.blank_pages ? ` · ${zh ? "空白" : "Blank"} ${summary.blank_pages}` : ""}</p>
            </> : active ? <p className="text-xs text-muted-foreground">{zh ? "总页数确认中" : "Checking page count"}</p> : null}
            {oldVersion ? <p className="text-xs text-muted-foreground">{zh ? "仍可检索此前已保存版本" : "Previously saved version remains searchable"}</p> : null}
            <KnowledgeIngestionStatus documentId={doc.id} status={doc.status} ingestion={summary} zh={zh}
              poll={false} detailsLabel={zh ? "查看识别详情" : "Recognition details"} onChange={() => void activity.refetch()} />
          </div>
        </li>;
      })}
    </ul>
    {!compact && data && data.total > 0 ? <div className="mt-3 flex items-center justify-between gap-3 text-xs text-muted-foreground">
      <span>{zh ? `共 ${data.total} 本` : `${data.total} books`}</span>
      <div className="flex items-center gap-3">
        <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label={zh ? "上一页" : "Previous page"} title={zh ? "上一页" : "Previous page"} className="h-8 w-8 rounded border p-1.5 disabled:opacity-40"><ChevronLeft className="h-4 w-4" /></button>
        <span>{page}/{pages}</span>
        <button type="button" disabled={page >= pages} onClick={() => setPage(page + 1)} aria-label={zh ? "下一页" : "Next page"} title={zh ? "下一页" : "Next page"} className="h-8 w-8 rounded border p-1.5 disabled:opacity-40"><ChevronRight className="h-4 w-4" /></button>
      </div>
    </div> : null}
  </section>;
}

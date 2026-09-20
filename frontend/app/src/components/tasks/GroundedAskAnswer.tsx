import { lazy, Suspense, useEffect, useState } from "react";
import type { GroundedAskExecution } from "@/types";
const GroundedChart = lazy(() => import("./GroundedChart").then(module => ({default: module.GroundedChart})));

export function GroundedAskAnswer({ execution, locale, onClarify, renderChart = true }: {
  execution?: GroundedAskExecution | null; locale: string; onClarify?: (text: string) => void; renderChart?: boolean;
}) {
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [execution]);
  if (!execution) return null;
  const zh = locale === "zh-CN", { data } = execution;
  const pages = Math.max(1, Math.ceil(data.rows.length / 25));
  const people = execution.bindings?.flatMap(binding => binding.rows.map(row => row.student_id ? `${row.student_name ?? ""} (${row.student_id})` : row.q_id ? String(row.number || row.q_id) : String(row.name || row.task_id))) ?? [];
  return <section className="mt-3 min-w-0 rounded-lg border bg-card p-3" data-grounded-answer>
    <p role="status" className={execution.recognized ? "text-xs text-muted-foreground" : "text-xs text-amber-700"}>{execution.explanation}</p>
    {people.length > 0 ? <p className="mt-2 text-xs font-semibold">{zh ? "匹配对象：" : "Matched: "}{[...new Set(people)].join(" · ")}</p> : null}
    {execution.assumptions?.map((a, i) => <p key={i} className="mt-1 text-xs text-muted-foreground">{a}</p>)}
    {renderChart && execution.chart ? <Suspense fallback={<p className="mt-2 text-xs">{zh ? "正在加载图表…" : "Loading chart…"}</p>}><GroundedChart result={execution.chart} locale={locale} /></Suspense> : null}
    {execution.recognized && !execution.selection && data.columns.length > 0 ? <div className="mt-3 overflow-x-auto">
      <table className="min-w-full text-left text-xs"><thead><tr>{data.columns.map(c => <th key={c} className="border-b p-2">{c}</th>)}</tr></thead>
        <tbody>{data.rows.slice(page * 25, (page + 1) * 25).map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j} className="max-w-sm whitespace-pre-wrap break-words border-b p-2">{v === null ? "—" : String(v)}</td>)}</tr>)}</tbody>
      </table>
      {data.rows.length === 0 ? <p className="py-3">{zh ? "没有匹配记录。" : "No matching records."}</p> : null}
      {pages > 1 ? <div className="mt-2 flex items-center gap-3"><button type="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>{zh ? "上一页" : "Previous"}</button><span>{page + 1}/{pages} · {data.rows.length}</span><button type="button" disabled={page + 1 >= pages} onClick={() => setPage(p => p + 1)}>{zh ? "下一页" : "Next"}</button></div> : null}
    </div> : null}
    {execution.sql ? <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">{zh ? "查看查询依据" : "Query evidence"}</summary><pre className="mt-2 overflow-auto whitespace-pre-wrap">{execution.sql}</pre><p className="mt-1 break-all">{zh ? "数据快照：" : "Snapshot: "}{execution.fingerprint}</p></details> : null}
  </section>;
}

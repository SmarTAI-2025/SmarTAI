import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, AlertTriangle, CheckCircle2, ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";

export interface ReviewQueueEntry {
  key: string;
  title: string;
  reason: string;
  href: string;
  severity: "blocking" | "advisory";
}

/** A queue item is one question (or student identity), never one source/page/error. */
export function ReviewQueue({ items, locale, confirmation }: { items: ReviewQueueEntry[]; locale: string; confirmation: ReactNode }) {
  const id = useId();
  const [selection, setSelection] = useState<"blocking" | "advisory" | null>(null);
  const required = items.filter(item => item.severity === "blocking");
  const suggested = items.filter(item => item.severity === "advisory");
  const active = selection ?? (required.length ? "blocking" : "advisory");
  const visible = active === "blocking" ? required : suggested;
  const zh = locale === "zh-CN";
  return <section className="flex h-[330px] min-w-0 flex-col overflow-hidden rounded-[10px] border bg-card px-4 py-5" aria-label={zh ? "待复核队列" : "Review queue"}>
    <div role="tablist" aria-label={zh ? "复核分类" : "Review categories"} className="flex shrink-0 gap-2 border-b pb-2">
      {(["blocking", "advisory"] as const).map(severity => <button key={severity} id={`${id}-${severity}`} type="button" role="tab" aria-selected={active === severity} aria-controls={`${id}-panel`} onClick={() => setSelection(severity)}
        className={cn("min-h-9 flex-1 rounded-md px-2 py-1 text-xs font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring", active === severity ? severity === "blocking" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800" : "text-muted-foreground hover:bg-muted")}>
        {severity === "blocking" ? (zh ? "需要处理" : "Action required") : (zh ? "建议处理" : "Suggested review")} ({severity === "blocking" ? required.length : suggested.length})
      </button>)}
    </div>
    <div id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-${active}`} className="flex min-h-0 flex-1 flex-col">
      {visible.length ? <ol className="mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto overscroll-contain pr-1">
        {visible.map(item => <li key={item.key}><Link to={item.href} className="flex min-h-[54px] items-center gap-2 rounded-lg px-2 py-1.5 text-xs hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">
          <span className="min-w-0 flex-1"><span className="block font-semibold">{item.title}</span><span className={cn("mt-0.5 block", item.severity === "blocking" ? "text-red-700" : "text-amber-700")}>{item.reason}</span></span>
          <ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0" />
        </Link></li>)}
      </ol> : <div className="flex flex-1 flex-col items-center justify-center text-center"><CheckCircle2 aria-hidden="true" className="h-7 w-7 text-teal-500" /><p className="mt-3 text-xs text-muted-foreground">{zh ? "当前没有待处理项。" : "No pending items."}</p></div>}
      {active === "advisory" && suggested.length > 0 ? <div className="mt-auto flex justify-end border-t pt-3">{confirmation}</div> : null}
    </div>
  </section>;
}

export function ReviewIssueNotes({ issues }: { issues: Array<{ label: string; severity: "blocking" | "advisory" }> }) {
  if (!issues.length) return null;
  return <ul className="w-full space-y-1 text-[11px] leading-5" data-review-issue-notes>{issues.map(({ label, severity }, index) => {
    const Icon = severity === "blocking" ? AlertCircle : AlertTriangle;
    return <li key={`${index}:${label}`} className={cn("flex items-start gap-1.5", severity === "blocking" ? "text-red-700" : "text-amber-700")}><Icon aria-hidden="true" className="mt-1 h-3.5 w-3.5 shrink-0" /><span>{label}</span></li>;
  })}</ul>;
}

import { LoaderCircle, X } from "lucide-react";
import { useState } from "react";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { TaskFilterFeedback } from "@/components/tasks/TaskFilterFeedback";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { TaskFilterController } from "@/hooks/useTaskFilterIntent";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type { ReviewSearchMatch } from "@/lib/reviewDetail";

export function ResultQuestionSearch({ className, locale, value, matches, onQuery, onSelect, filter, taskId }: {
  className?: string; locale: Locale; value: string; matches: ReviewSearchMatch[];
  onQuery: (value: string) => void; onSelect: (id: string) => void; filter: TaskFilterController; taskId?: string;
}) {
  const [open, setOpen] = useState(false);
  const search = useImeSafeQuery({ value, onCommit: onQuery, onDraftChange: () => setOpen(true) });
  const zh = locale === "zh-CN";
  return <section className={cn("relative rounded-[10px] border bg-card p-2", className)} aria-label={zh ? "题目筛选" : "Question filter"}>
    <form className="flex items-center gap-2" onSubmit={(event) => { event.preventDefault(); const query = search.commitDraft(); void filter.apply(query); setOpen(true); }}>
      <SmarTAIMascot variant={filter.pending ? "grading" : "thinking"} size="xs" />
      <label className="relative min-w-0 flex-1">
        <span className="sr-only">{zh ? "Ask SmarTAI：筛选与排序题目" : "Ask SmarTAI: filter and sort questions"}</span>
        <input value={search.draftValue} inputMode="search" onFocus={() => setOpen(true)} onCompositionStart={search.handleCompositionStart} onCompositionEnd={search.handleCompositionEnd}
          onChange={(event) => { filter.cancel(); search.handleChange(event); }} onBlur={(event) => { search.handleBlur(event); window.setTimeout(() => setOpen(false), 120); }}
          placeholder={zh ? "Ask SmarTAI：找出低置信题目，按得分率排序…" : "Ask SmarTAI: show low-confidence questions, sort by score…"}
          className="h-10 w-full rounded-[7px] bg-muted/50 pl-3 pr-9 text-[13px] outline-none focus:ring-2 focus:ring-primary/20" />
        {search.draftValue ? <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { filter.cancel(); search.commitValue(""); setOpen(false); }} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground" aria-label={zh ? "清空题目筛选" : "Clear question filter"}><X aria-hidden="true" className="h-4 w-4" /></button> : null}
      </label>
      <button type="submit" disabled={filter.pending || !search.draftValue.trim()} className="inline-flex h-9 shrink-0 items-center gap-1 rounded-md bg-primary px-3 text-xs font-semibold text-primary-foreground disabled:opacity-50">{filter.pending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}{zh ? "应用筛选" : "Apply filter"}</button>
    </form>
    <TaskFilterFeedback filter={filter} taskId={taskId} />
    {open && value.trim() && filter.intent && !filter.pending ? <div className="absolute left-2 right-2 top-full z-40 max-h-64 overflow-auto rounded-lg border bg-card p-1.5 shadow-xl">
      {matches.length ? matches.slice(0, 20).map((match) => <button key={match.item.id} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { onSelect(match.item.id); setOpen(false); }} className="flex min-h-10 w-full items-center gap-3 rounded-md px-3 py-2 text-left hover:bg-muted"><span className="min-w-12 text-xs font-bold">{match.item.primary}</span><span className="truncate text-xs text-muted-foreground">{match.item.secondary}</span></button>) : <p className="px-3 py-5 text-center text-xs text-muted-foreground">{zh ? "没有匹配题目；清空后可恢复全部。" : "No questions matched; clear the filter to restore all."}</p>}
    </div> : null}
  </section>;
}

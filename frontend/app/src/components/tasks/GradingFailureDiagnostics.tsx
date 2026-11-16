import { correctionReviewNotes } from "./resultsReviewModel";
import type { JobProgress, Task } from "@/types";
import type { Locale } from "@/i18n/messages";

export function GradingFailureDiagnostics({ progress, task, locale }: { progress?: JobProgress | null; task?: Task; locale: Locale }) {
  const required = (progress?.grading_review ?? []).map(row => ({ row,
    notes: correctionReviewNotes(row, locale).filter(note => note.severity === "blocking") }))
    .filter(item => item.notes.length > 0);
  const rows = required.slice(0, 20);
  if (!rows.length) return null;
  const zh = locale === "zh-CN";
  const total = progress?.grading_review_summary?.affected ?? required.length;
  const missing = progress?.grading_review_summary?.missing_scores ?? required.filter(({ row }) => row.score === null).length;
  return <section className="my-4 rounded-lg border bg-card p-5" aria-label={zh ? "批改问题明细" : "Grading issue details"}>
    <h2 className="font-semibold">{zh ? `有 ${total} 份作答需要关注，其中 ${missing} 份缺少有效分数` : `${total} responses need attention; ${missing} have no valid score`}</h2>
    <ul className="mt-3 max-h-48 space-y-3 overflow-y-auto">
      {rows.map(({ row, notes }) => {
        const student = task?.student_data?.[row.student_id];
        const identity = [student?.stu_name, student?.stu_id || row.student_id]
          .map(value => value?.trim()).filter((value): value is string => Boolean(value) && !value!.startsWith("imported_") && value !== "[Unknown Student]");
        const studentLabel = [...new Set(identity)].join(zh ? "（" : " (") + (new Set(identity).size > 1 ? (zh ? "）" : ")") : "");
        const question = task?.problem_data?.[row.q_id];
        return <li key={`${row.student_id}:${row.q_id}`} className="rounded-md border px-3 py-2 text-sm">
          <strong>{studentLabel || (zh ? "身份待确认" : "Identity needs confirmation")} · {zh ? `第 ${question?.number || row.q_id} 题` : `Question ${question?.number || row.q_id}`}</strong>
          {notes.map(note => <p key={note.label} className={note.severity === "blocking" ? "mt-1 text-red-700 dark:text-red-300" : "mt-1 text-amber-700 dark:text-amber-300"}>
            {zh ? (note.severity === "blocking" ? "需处理：" : "建议复核：") : (note.severity === "blocking" ? "Required: " : "Suggested: ")}{note.label}
          </p>)}
        </li>;
      })}
    </ul>
    {total > rows.length ? <p className="mt-3 text-xs text-muted-foreground">{zh ? `先显示 ${rows.length} 份作答，进入复核可查看全部。` : `Showing ${rows.length} responses. Open review to see all items.`}</p> : null}
  </section>;
}

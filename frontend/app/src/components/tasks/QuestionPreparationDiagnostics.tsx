import type { JobProgress } from "@/types";
import { recognitionIssueLabel } from "@/lib/reviewConfirmation";
import { questionIssueCodeLabel } from "@/lib/reviewLabels";

/** Same field-level explanations as the review pages, before manual handoff. */
export function QuestionPreparationDiagnostics({ progress, locale }: { progress: JobProgress; locale: string }) {
  const rows = progress.preparation_review ?? [];
  if (!rows.length) return null;
  const zh = locale === "zh-CN";
  const counts = progress.preparation_counts;
  const required = progress.preparation_review_summary?.blocking ?? rows.filter(row => row.issues.some(issue => issue.severity === "blocking")).length;
  const suggested = progress.preparation_review_summary?.advisory ?? rows.filter(row => row.issues.some(issue => issue.severity === "warning")).length;
  return <section aria-label={zh ? "题目待处理详情" : "Question preparation details"} className="rounded-lg border bg-card p-4 text-sm">
    <h3 className="font-semibold">{zh ? `需要处理 ${required} 题 · 建议复核 ${suggested} 题` : `${required} questions need action · ${suggested} need suggested review`}</h3>
    {counts && <p className="mt-1 text-muted-foreground">{zh
      ? `已保存 ${counts.stems} 个题干、${counts.reference_answers} 份标答、${counts.rubrics} 份评分标准（共 ${counts.questions} 题）。`
      : `Saved ${counts.stems} question texts, ${counts.reference_answers} reference answers and ${counts.rubrics} rubrics (${counts.questions} questions).`}</p>}
    <ul className="mt-3 grid gap-3">{rows.map(row => {
      const notes = new Map<string, string>();
      for (const issue of row.issues) {
        const label = recognitionIssueLabel(issue, locale) ?? questionIssueCodeLabel(issue.code, locale);
        if (!notes.has(label) || issue.severity === "blocking") notes.set(label, issue.severity);
      }
      return <li key={row.q_id}><strong>{zh ? `第 ${row.number || "未编号"} 题` : `Question ${row.number || "unnumbered"}`}</strong>
        <ul className="mt-1 grid gap-1">{[...notes].sort((a, b) => Number(b[1] === "blocking") - Number(a[1] === "blocking")).map(([label, severity]) => <li key={label}
          className={severity === "blocking" ? "text-danger" : "text-amber-700 dark:text-amber-400"}>
          {severity === "blocking" ? (zh ? "需处理：" : "Required: ") : (zh ? "建议复核：" : "Suggested: ")}{label}
        </li>)}</ul>
      </li>;
    })}</ul>
    {(progress.preparation_review_summary?.questions ?? 0) > rows.length && <p className="mt-3">{zh
      ? `这里显示 ${rows.length} 题；人工继续处理后可查看全部 ${progress.preparation_review_summary!.questions} 题的待处理事项。`
      : `Showing ${rows.length} questions; continue manually to see all ${progress.preparation_review_summary!.questions} questions needing attention.`}</p>}
    <p className="mt-3 text-muted-foreground">{zh ? "可重试未完成项，或选择人工继续处理，保留已保存的资料。" : "Retry incomplete items or continue manually with saved materials."}</p>
  </section>;
}

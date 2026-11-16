import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ScoreRoundingNotes } from "@/components/tasks/ScoreRoundingNotes";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { aiCorrectionScore, buildResultsModel, formatScore, formatScoringRow, scoringRows, validExpertResults } from "@/components/tasks/resultsModel";
import type { Locale } from "@/i18n/messages";
import type { ExpertResult, StepScore, TaskResultResponse } from "@/types";

export type DemoContentStage = "materials" | "submissions" | "results";
type ContentTab = "materials" | "submissions" | "grading" | "analysis";

/** The parent keys this view by task and available stage: stage completion
 * selects its new content once, while polling and tab clicks never restart work. */
export function DemoContentTabs({ stage, locale, materials, submissions, result, insights, taskId }: {
  stage: DemoContentStage;
  locale: Locale;
  materials: ReactNode;
  submissions: ReactNode;
  result: TaskResultResponse | null;
  insights: ReactNode;
  taskId: string;
}) {
  const id = useId();
  const zh = locale === "zh-CN";
  const [active, setActive] = useState<ContentTab>(stage === "results" ? "grading" : stage);
  const tabs: Array<{ key: ContentTab; label: string; content: ReactNode }> = [
    { key: "materials", label: zh ? "题目资料" : "Materials", content: materials },
    ...(stage !== "materials" ? [{ key: "submissions" as const, label: zh ? "学生作答" : "Student answers", content: submissions }] : []),
    ...(result ? [
      { key: "grading" as const, label: zh ? "批改详情" : "Grading details", content: <DemoGradingDetails result={result} taskId={taskId} locale={locale} /> },
      { key: "analysis" as const, label: zh ? "学情分析" : "Learning analysis", content: insights },
    ] : []),
  ];
  function navigateTabs(event: KeyboardEvent<HTMLDivElement>) {
    const current = tabs.findIndex(tab => tab.key === active);
    const next = event.key === "ArrowRight" ? (current + 1) % tabs.length
      : event.key === "ArrowLeft" ? (current + tabs.length - 1) % tabs.length
      : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    setActive(tabs[next].key);
    event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=tab]")[next]?.focus();
  }
  return <section className="demo-content-tabs mt-5" data-analysis={active === "analysis" || undefined} aria-label={zh ? "演示内容" : "Demo content"}>
    <div className="demo-content-tabs__list" role="tablist" aria-label={zh ? "演示内容" : "Demo content"} onKeyDown={navigateTabs}>
      {tabs.map(tab => <button key={tab.key} id={`${id}-${tab.key}`} type="button" role="tab" aria-selected={active === tab.key}
        aria-controls={`${id}-${tab.key}-panel`} tabIndex={active === tab.key ? 0 : -1}
        className="demo-content-tabs__tab" onClick={() => setActive(tab.key)}>{tab.label}</button>)}
    </div>
    <div className="demo-content-tabs__body">
      {tabs.map(tab => <div key={tab.key} id={`${id}-${tab.key}-panel`} role="tabpanel" aria-labelledby={`${id}-${tab.key}`} hidden={active !== tab.key}>{tab.content}</div>)}
    </div>
  </section>;
}

function feedbackParagraphs(text = "") {
  const normalized = text.replace(/\r\n?/g, "\n").trim();
  // Keep fenced code and display-math blocks intact, including blank lines.
  if (/(^|\n)[ \t]*(?:`{3,}|~{3,}|\$\$|\\\[)/.test(normalized)) return normalized ? [normalized] : [];
  return normalized.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean);
}

/** Prefer attributed expert text; retain distinct summary and expert paragraphs. */
function demoFeedback(summary: string, experts: ExpertResult[]) {
  const shown = new Set<string>();
  const comments = experts.map(expert => {
    const parts = feedbackParagraphs(expert.comment);
    const remaining = parts.filter(part => !shown.has(part));
    parts.forEach(part => shown.add(part));
    return remaining.join("\n\n");
  });
  const remaining = (text: string) => feedbackParagraphs(text).filter(part =>
    ![...shown].some(text => text === part || text.startsWith(part + "\n") || text.endsWith("\n" + part))).join("\n\n");
  // The weighted fallback wraps each saved comment in this exact provider header.
  // Remove that wrapper only for a known expert, never a guessed prose heading.
  const distinctSummary = summary.split(/\n\n———\n\n/).map(block => {
    const lines = block.split("\n");
    if (experts.some(expert => lines[0].startsWith(`【${expert.provider}（`) && lines[0].endsWith("）】"))) {
      const body = remaining(lines.slice(1).join("\n"));
      return body ? `${lines[0]}\n${body}` : "";
    }
    return remaining(block);
  }).filter(Boolean).join("\n\n");
  return { summary: distinctSummary, comments };
}

function DemoGradingDetails({ result, taskId, locale }: { result: TaskResultResponse; taskId: string; locale: Locale }) {
  const zh = locale === "zh-CN";
  const model = buildResultsModel(undefined, result);
  const labels = new Map(model.questions.map(question => [question.id, question.label]));
  return <section aria-labelledby="demo-grading-details-title">
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <h3 id="demo-grading-details-title" className="text-lg font-bold">{zh ? "本次批改的具体情况" : "Detailed grading results"}</h3>
      <Link className="shrink-0 text-xs font-semibold text-primary hover:underline" to={`/tasks/${taskId}/review`}>{zh ? "在批改复核页查看" : "Open grading review"}</Link>
    </div>
    <div className="mt-4 grid items-start gap-3 md:grid-cols-2">
      {model.students.map(student => {
        const scores = student.corrections.map(aiCorrectionScore);
        const total = scores.length && scores.every(score => score !== null) ? scores.reduce<number>((sum, score) => sum + (score ?? 0), 0) : null;
        const max = student.corrections.reduce((sum, correction) => sum + correction.max_score, 0);
        return <article key={student.id} className="rounded-lg border bg-card px-3 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0"><p className="truncate text-sm font-bold">{student.name}</p><p className="mt-0.5 truncate text-[11px] text-muted-foreground">{student.id}</p></div>
            {total !== null ? <span className="shrink-0 rounded-full bg-primary/5 px-2 py-1 text-[11px] font-semibold text-primary">{formatScore(total)} / {formatScore(max)}</span> : null}
          </div>
          <div className="mt-3 grid gap-2 border-t pt-3">
            {student.corrections.map((correction, index) => {
              const experts = validExpertResults(correction);
              const feedback = demoFeedback(correction.comment ?? "", experts);
              return <section key={`${correction.q_id}-${index}`} className="rounded-md bg-muted/45 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-bold text-primary">{labels.get(correction.q_id) ?? correction.q_id}</span>
                <span className="shrink-0 rounded-full bg-primary/5 px-2 py-1 text-[11px] font-semibold text-primary">{scores[index] === null ? (zh ? "无有效分数" : "No valid score") : `${formatScore(scores[index])} / ${formatScore(correction.max_score)}`}</span>
              </div>
              {feedback.summary || !feedback.comments.some(Boolean) ? <MarkdownMath className="mt-2 text-[12px] leading-5 text-foreground">{feedback.summary || (zh ? "SmarTAI 未返回文字说明。" : "SmarTAI returned no written rationale.")}</MarkdownMath> : null}
              {experts.length ? experts.map((expert, expertIndex) =>
                <section key={`${expert.provider_id ?? expert.provider}-${expertIndex}`} className="mt-3 border-t pt-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-semibold">
                    <span className="min-w-0 break-words">{expert.provider}</span>
                    <span className="shrink-0">{formatScore(expert.score)} / {formatScore(expert.max_score ?? correction.max_score)}</span>
                  </div>
                  {feedback.comments[expertIndex] ? <MarkdownMath className="mt-1 text-[12px] leading-5">{feedback.comments[expertIndex]}</MarkdownMath> : null}
                  <DemoScoringEvidence steps={expert.steps} zh={zh} />
                </section>) : <DemoScoringEvidence steps={correction.steps} zh={zh} />}
            </section>; })}
          </div>
        </article>;
      })}
    </div>
  </section>;
}

function DemoScoringEvidence({ steps, zh }: { steps: StepScore[]; zh: boolean }) {
  const rows = scoringRows(steps);
  return <>
    {steps.some(step => step.is_alternative) ? <p className="mt-2 text-[11px] text-muted-foreground">{zh ? "另解批改" : "Alternative method"}</p> : null}
    {rows.length ? <details className="mt-3 border-t pt-2">
      <summary className="cursor-pointer text-[11px] font-semibold text-muted-foreground">{rows.every(row => row.deduction) ? (zh ? "扣分点" : "Deductions") : (zh ? "评分步骤" : "Scoring steps")}<span className="ml-2 font-normal">{rows.length} {zh ? "项" : "items"}</span></summary>
      <ol className="mt-2 space-y-2">{rows.map((step, index) => <li key={`${step.step_no}-${index}`} className="grid grid-cols-[24px_minmax(0,1fr)_auto] gap-2 rounded-[7px] bg-muted/55 px-3 py-2 text-[11px] leading-5">
        <span className="font-semibold text-muted-foreground">{index + 1}</span>
        <MarkdownMath className="min-w-0 text-foreground">{step.desc}</MarkdownMath>
        <span className={`font-semibold ${step.is_correct ? "text-accent" : "text-warning"}`}>{formatScoringRow(step)} {zh ? "分" : "pts"}</span>
      </li>)}</ol>
    </details> : null}
    <ScoreRoundingNotes steps={steps} zh={zh} />
  </>;
}

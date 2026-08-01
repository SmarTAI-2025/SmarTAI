import type { QuestionSummary } from "@/components/tasks/resultsModel";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";

export type ResultQuestionState = "ready" | "confirmed" | "warning" | "danger" | "muted";

export function ResultQuestionSidebar({
  locale,
  questions,
  activeId,
  onSelect,
  stateForQuestion,
  behavior = "switch",
}: {
  locale: Locale;
  questions: QuestionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  stateForQuestion?: (question: QuestionSummary) => ResultQuestionState;
  behavior?: "switch" | "locate";
}) {
  return (
    <aside
      className="sticky top-[86px] z-20 hidden max-h-[calc(100vh-102px)] overflow-hidden rounded-[10px] border bg-card lg:flex lg:flex-col"
      aria-label={tx(locale, "题目导航", "Question navigation")}
    >
      <div className="shrink-0 border-b px-3 py-3">
        <p className="text-xs font-bold text-foreground">{tx(locale, "题目导航", "Questions")}</p>
        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
          {behavior === "locate"
            ? tx(locale, `共 ${questions.length} 题 · 点击定位`, `${questions.length} questions · select to locate`)
            : tx(locale, `共 ${questions.length} 题 · 点击切换`, `${questions.length} questions · select to switch`)}
        </p>
      </div>
      <div className="min-h-0 overflow-y-auto p-2 overscroll-contain">
        {questions.map((question) => {
          const active = question.id === activeId;
          const state = stateForQuestion?.(question) ?? "ready";
          return (
            <button
              key={question.id}
              type="button"
              aria-current={active ? "true" : undefined}
              onClick={() => onSelect(question.id)}
              className={cn(
                "mb-1 flex min-h-10 w-full items-center justify-between rounded-[7px] px-2.5 text-left text-xs font-semibold transition last:mb-0",
                active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <span className="truncate">{question.label}</span>
              <span
                aria-hidden="true"
                className={cn(
                  "ml-1 h-2 w-2 shrink-0 rounded-full",
                  active && "bg-white",
                  !active && state === "ready" && "bg-emerald-500",
                  !active && state === "confirmed" && "bg-blue-500",
                  !active && state === "warning" && "bg-amber-500",
                  !active && state === "danger" && "bg-rose-500",
                  !active && state === "muted" && "bg-slate-300",
                )}
              />
            </button>
          );
        })}
        {!questions.length ? (
          <p className="px-2 py-5 text-center text-[11px] leading-4 text-muted-foreground">
            {tx(locale, "没有匹配题目", "No matching questions")}
          </p>
        ) : null}
      </div>
    </aside>
  );
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

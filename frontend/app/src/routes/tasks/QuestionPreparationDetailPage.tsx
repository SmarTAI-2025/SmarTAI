import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  Eye,
  EyeOff,
  Loader2,
  Pencil,
  Plus,
  Save,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { Link, Navigate, useBeforeUnload, useBlocker, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { useTask, useUpdateProblem } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { EmptyState } from "@/components/ui/EmptyState";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { SyntaxHighlightedCode } from "@/components/ui/SyntaxHighlightedCode";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { isProgrammingProblem } from "@/lib/questionPreparation";
import { questionSearchAliases } from "@/lib/questionSearch";
import type { ProblemInfo, TestCase } from "@/types";

type TextFieldKey = "stem" | "reference_answer" | "criterion" | "solution_code";

const EMPTY_TEST_CASES: TestCase[] = [];

export function QuestionPreparationDetailPage() {
  const { taskId, questionId } = useParams();
  const stableTaskId = taskId ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const taskQuery = useTask(taskId);
  const updateProblem = useUpdateProblem();
  const [activeQuestionId, setActiveQuestionId] = useState(questionId ?? "");
  const [dirtyKeys, setDirtyKeys] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const urlQuery = searchParams.get("q") ?? "";
  const [query, setQuery] = useState(urlQuery);
  const composingRef = useRef(false);
  const pendingCompositionCommitRef = useRef<number | null>(null);
  const lastCommittedQueryRef = useRef(urlQuery);
  const positionedPathRef = useRef<string | null>(null);

  const problems = useMemo(
    () => sortProblems(Object.values(taskQuery.data?.problem_data ?? {}), locale),
    [locale, taskQuery.data?.problem_data],
  );
  const filtered = useMemo(() => filterProblems(problems, urlQuery), [problems, urlQuery]);
  const activeQuestionIndex = filtered.findIndex((problem) => problem.q_id === activeQuestionId);
  const previousQuestion = activeQuestionIndex > 0 ? filtered[activeQuestionIndex - 1] : null;
  const nextQuestion = activeQuestionIndex >= 0 && activeQuestionIndex < filtered.length - 1
    ? filtered[activeQuestionIndex + 1]
    : null;
  const readOnly = Boolean(taskQuery.data && taskQuery.data.status !== "problems_ready");
  const hasDirty = dirtyKeys.size > 0;
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    hasDirty && currentLocation.pathname !== nextLocation.pathname
  ));

  useEffect(() => {
    lastCommittedQueryRef.current = urlQuery;
    if (!composingRef.current && pendingCompositionCommitRef.current === null) {
      setQuery((current) => current === urlQuery ? current : urlQuery);
    }
  }, [urlQuery]);

  useEffect(() => () => {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
    }
  }, []);

  useBeforeUnload(useCallback((event) => {
    if (hasDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  }, [hasDirty]));

  useEffect(() => {
    if (!filtered.length) return;
    const target = filtered.find((problem) => problem.q_id === questionId) ?? filtered[0];
    setActiveQuestionId(target.q_id);
    const pathKey = `${location.pathname}${location.hash}`;
    if (positionedPathRef.current === pathKey) return;
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        positionedPathRef.current = pathKey;
        const requestedAnchor = decodeURIComponent(location.hash.replace(/^#/, ""));
        if (requestedAnchor === questionAnchorId(target.q_id)) scrollQuestionIntoView(target.q_id, "auto");
        else window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [filtered, location.hash, location.pathname, questionId]);

  useEffect(() => {
    if (!filtered.length) return;
    let frame = 0;
    const updateActiveQuestion = () => {
      frame = 0;
      let active = filtered[0]?.q_id ?? "";
      const scrollRoot = document.scrollingElement ?? document.documentElement;
      const reachedDocumentEnd = scrollRoot.scrollHeight > window.innerHeight + 1
        && window.scrollY + window.innerHeight >= scrollRoot.scrollHeight - 2;
      if (reachedDocumentEnd) {
        active = filtered[filtered.length - 1]?.q_id ?? active;
      } else {
        for (const problem of filtered) {
          const element = document.getElementById(questionAnchorId(problem.q_id));
          if (!element || element.getBoundingClientRect().top > 112) break;
          active = problem.q_id;
        }
      }
      if (active) setActiveQuestionId(active);
    };
    const scheduleUpdate = () => {
      if (!frame) frame = window.requestAnimationFrame(updateActiveQuestion);
    };
    updateActiveQuestion();
    window.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    return () => {
      window.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [filtered]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (isKeyboardNavigationBlocked(event.target)) return;
      if (event.key === "ArrowUp" && previousQuestion) {
        event.preventDefault();
        scrollToQuestion(previousQuestion.q_id);
      } else if (event.key === "ArrowDown" && nextQuestion) {
        event.preventDefault();
        scrollToQuestion(nextQuestion.q_id);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [nextQuestion?.q_id, previousQuestion?.q_id]);

  if (!taskId) {
    return <EmptyState title={tx(locale, "缺少任务 ID", "Task ID is missing")} description={tx(locale, "请从题目风险总览重新进入。", "Reopen this page from the risk overview.")} />;
  }
  if (taskQuery.data?.status === "draft") return <Navigate to={`/tasks/${taskId}/upload/problems`} replace />;
  if (taskQuery.data?.status === "extracting_problems") return <Navigate to={`/tasks/${taskId}/problems/progress`} replace />;

  function updateQuery(value: string) {
    if (lastCommittedQueryRef.current === value) return;
    lastCommittedQueryRef.current = value;
    const next = new URLSearchParams(searchParams);
    if (value.trim()) next.set("q", value);
    else next.delete("q");
    setSearchParams(next, { replace: true });
  }

  function flushComposition(input: HTMLInputElement) {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    composingRef.current = false;
    const finalValue = input.value;
    setQuery(finalValue);
    updateQuery(finalValue);
  }

  const setFieldDirty = useCallback((key: string, dirty: boolean) => {
    setDirtyKeys((current) => {
      const next = new Set(current);
      if (dirty) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  function scrollToQuestion(targetId: string) {
    setActiveQuestionId(targetId);
    scrollQuestionIntoView(targetId, "smooth");
  }

  const overviewHref = `/tasks/${encodeURIComponent(taskId)}/questions${searchParams.size ? `?${searchParams.toString()}` : ""}`;

  async function saveText(problem: ProblemInfo, field: TextFieldKey, value: string) {
    await updateProblem.mutateAsync({ taskId: stableTaskId, qId: problem.q_id, [field]: value });
  }

  async function saveTests(problem: ProblemInfo, cases: TestCase[]) {
    await updateProblem.mutateAsync({ taskId: stableTaskId, qId: problem.q_id, test_cases: cases });
  }

  async function saveMaxScore(problem: ProblemInfo, maxScore: number) {
    await updateProblem.mutateAsync({ taskId: stableTaskId, qId: problem.q_id, max_score: maxScore });
  }

  async function confirmAll() {
    if (hasDirty) {
      toast.error(tx(locale, "请先保存或取消正在编辑的内容。", "Save or cancel the active edits first."));
      return;
    }
    setConfirming(true);
    try {
      for (const problem of problems) {
        await updateProblem.mutateAsync({
          taskId: stableTaskId,
          qId: problem.q_id,
          stem: problem.stem,
          criterion: problem.criterion,
          reference_answer: problem.reference_answer ?? "",
          ...(isProgrammingProblem(problem) ? {
            solution_code: problem.solution_code ?? "",
            test_cases: problem.test_cases ?? [],
          } : {}),
          review_status: "confirmed",
        });
      }
      toast.success(tx(locale, "全部题目资料已确认。", "All question materials are confirmed."));
      navigate(`/tasks/${taskId}/submissions/upload`);
    } catch {
      toast.error(tx(locale, "确认失败，请刷新后重试。", "Confirmation failed. Refresh and retry."));
    } finally {
      setConfirming(false);
    }
  }

  return (
    <div className="w-full max-w-[1300px]">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">{tx(locale, "题目资料审核", "Review Question Materials")}</h1>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{tx(locale, "连续浏览每道题的题目、标答和评分标准；只有编程题显示测试样例。", "Review each question, reference answer, and rubric together. Test cases appear only for programming questions.")}</p>
        </div>
        <span className="text-xs text-muted-foreground">{taskQuery.data?.name ?? ""}</span>
      </div>
      <NewTaskStepper currentStep={2} />

      <label className="relative mt-6 block">
        <span className="sr-only">{tx(locale, "SmarTAI 智能筛选题目", "SmarTAI Smart question filter")}</span>
        <Search aria-hidden="true" className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <input
          value={query}
          inputMode="search"
          onCompositionStart={() => {
            if (pendingCompositionCommitRef.current !== null) {
              window.clearTimeout(pendingCompositionCommitRef.current);
              pendingCompositionCommitRef.current = null;
            }
            composingRef.current = true;
          }}
          onCompositionEnd={(event) => {
            const input = event.currentTarget;
            composingRef.current = false;
            setQuery(input.value);
            pendingCompositionCommitRef.current = window.setTimeout(() => {
              flushComposition(input);
            }, 0);
          }}
          onChange={(event) => {
            const value = event.currentTarget.value;
            setQuery(value);
            if (
              !composingRef.current
              && pendingCompositionCommitRef.current === null
              && !(event.nativeEvent as InputEvent).isComposing
            ) {
              updateQuery(value);
            }
          }}
          onBlur={(event) => {
            if (composingRef.current || pendingCompositionCommitRef.current !== null) {
              flushComposition(event.currentTarget);
            }
          }}
          placeholder={tx(locale, "SmarTAI 智能搜索：题号、题型、题目内容，或“编程题 / 低置信 / 冲突”", "SmarTAI Smart Search: number, type, content, or “programming / low confidence / conflict”")}
          className="h-12 w-full rounded-[10px] border bg-card pl-11 pr-4 text-[13px] outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
        />
      </label>

      {readOnly ? <p className="mt-4 rounded-[8px] border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200">{tx(locale, "当前任务已进入后续阶段，本页可浏览但不能修改。", "This task has moved to a later stage. The page is read-only.")}</p> : null}

      {taskQuery.isLoading ? (
        <div className="mt-5 flex min-h-80 items-center justify-center rounded-[10px] border bg-card"><Loader2 aria-hidden="true" className="h-6 w-6 animate-spin text-primary" /></div>
      ) : taskQuery.isError ? (
        <EmptyState title={tx(locale, "无法读取题目", "Questions could not be loaded")} description={tx(locale, "请检查连接后重试。", "Check the connection and retry.")} />
      ) : filtered.length === 0 ? (
        <EmptyState title={tx(locale, "没有匹配的题目", "No matching questions")} description={tx(locale, "清空或调整筛选条件。", "Clear or adjust the filter.")} />
      ) : (
        <div className="mt-5 grid items-start gap-4 lg:grid-cols-[132px_minmax(0,1fr)]">
          <aside className="sticky top-[86px] z-20 hidden max-h-[calc(100vh-102px)] overflow-hidden rounded-[10px] border bg-card lg:flex lg:flex-col" aria-label={tx(locale, "题目导航", "Question navigation")}>
            <div className="shrink-0 border-b px-3 py-3">
              <p className="text-xs font-bold text-foreground">{tx(locale, "题目导航", "Questions")}</p>
              <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{tx(locale, `共 ${filtered.length} 题 · 点击定位`, `${filtered.length} questions · select to locate`)}</p>
            </div>
            <div className="min-h-0 overflow-y-auto p-2 overscroll-contain">
              {filtered.map((problem) => {
                const active = problem.q_id === activeQuestionId;
                const riskCount = openRiskCount(problem);
                const number = problem.number || problem.q_id;
                return (
                  <button key={problem.q_id} type="button" aria-current={active ? "true" : undefined} onClick={() => scrollToQuestion(problem.q_id)} className={cn("mb-1 flex min-h-10 w-full items-center justify-between rounded-[7px] px-2.5 text-left text-xs font-semibold transition last:mb-0", active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground")}>
                    <span className="truncate">{tx(locale, `第 ${number} 题`, `Q${number}`)}</span>
                    {riskCount ? <span className={cn("ml-1 inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1 text-[10px]", active ? "bg-white/20 text-white" : "bg-amber-100 text-amber-700")}>{riskCount}</span> : null}
                  </button>
                );
              })}
            </div>
          </aside>

          <main className="min-w-0 space-y-5">
            {filtered.map((problem, index) => (
              <QuestionPackageCard
                key={problem.q_id}
                problem={problem}
                index={index}
                total={filtered.length}
                previous={filtered[index - 1] ?? null}
                next={filtered[index + 1] ?? null}
                readOnly={readOnly}
                saving={updateProblem.isPending}
                locale={locale}
                onDirtyChange={setFieldDirty}
                onSaveMaxScore={saveMaxScore}
                onSaveText={saveText}
                onSaveTests={saveTests}
                onNavigate={scrollToQuestion}
              />
            ))}

            <section className="rounded-[10px] border bg-card p-5 sm:p-6">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 className="text-lg font-bold text-foreground">{tx(locale, "完成全部题目资料审核", "Finish Reviewing All Question Materials")}</h2>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">{tx(locale, `共 ${problems.length} 道题；确认后进入学生作答上传。`, `${problems.length} ${problems.length === 1 ? "question" : "questions"}; continue to student submissions after confirmation.`)}</p>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Link to={overviewHref} className="inline-flex h-10 items-center justify-center gap-2 rounded-[8px] border bg-card px-4 text-sm font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">
                    <ArrowLeft aria-hidden="true" className="h-4 w-4" />
                    {tx(locale, "返回题目资料总览", "Back to Question Material Overview")}
                  </Link>
                  {!readOnly ? <button type="button" disabled={confirming || hasDirty} onClick={() => void confirmAll()} className="inline-flex h-10 items-center justify-center gap-2 rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
                    {confirming ? <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Check aria-hidden="true" className="h-4 w-4" />}
                    {tx(locale, "确认全部题目资料", "Confirm All Materials")}
                  </button> : null}
                </div>
              </div>
            </section>
          </main>
        </div>
      )}

      {blocker.state === "blocked" ? (
        <UnsavedChangesDialog
          title={tx(locale, "离开且不保存？", "Leave without saving?")}
          description={tx(locale, "当前页面还有未保存的题目资料修改。", "This page still has unsaved question material edits.")}
          stayLabel={tx(locale, "继续编辑", "Keep Editing")}
          leaveLabel={tx(locale, "放弃修改", "Discard Changes")}
          onStay={() => blocker.reset()}
          onLeave={() => blocker.proceed()}
        />
      ) : null}
    </div>
  );
}

function QuestionPackageCard({ problem, index, total, previous, next, readOnly, saving, locale, onDirtyChange, onSaveMaxScore, onSaveText, onSaveTests, onNavigate }: {
  problem: ProblemInfo;
  index: number;
  total: number;
  previous: ProblemInfo | null;
  next: ProblemInfo | null;
  readOnly: boolean;
  saving: boolean;
  locale: string;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSaveMaxScore: (problem: ProblemInfo, maxScore: number) => Promise<void>;
  onSaveText: (problem: ProblemInfo, field: TextFieldKey, value: string) => Promise<void>;
  onSaveTests: (problem: ProblemInfo, cases: TestCase[]) => Promise<void>;
  onNavigate: (qId: string) => void;
}) {
  const programming = isProgrammingProblem(problem);
  const risks = (problem.preparation_issues ?? []).filter((issue) => issue.status === "open");
  return (
    <article id={questionAnchorId(problem.q_id)} data-question-id={problem.q_id} className="scroll-mt-[86px] overflow-hidden rounded-[10px] border bg-card">
      <header className="flex flex-col gap-3 border-b px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-bold text-foreground">{tx(locale, `第 ${problem.number || problem.q_id} 题`, `Question ${problem.number || problem.q_id}`)}</h2>
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-muted-foreground dark:bg-slate-800">{problem.type || tx(locale, "未分类", "Uncategorized")}</span>
            <span className={cn("rounded-full px-2.5 py-1 text-xs font-semibold", problem.max_score_review_status === "confirmed" ? "bg-blue-50 text-primary dark:bg-blue-950/35" : "bg-amber-100 text-amber-700 dark:bg-amber-950/35 dark:text-amber-300")}>{tx(locale, `满分 ${formatScore(problem.max_score ?? 10)} 分`, `${formatScore(problem.max_score ?? 10)} points max`)}</span>
            {risks.length ? <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-700">{tx(locale, `${risks.length} 项需核对`, `${risks.length} ${risks.length === 1 ? "risk" : "risks"}`)}</span> : null}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{tx(locale, `筛选结果中的第 ${index + 1} / ${total} 题`, `${index + 1} of ${total}`)}</p>
        </div>
        <QuestionNavigator previous={previous} next={next} locale={locale} onNavigate={onNavigate} />
      </header>

      {risks.length ? (
        <div className="border-b bg-amber-50/70 px-5 py-3 dark:bg-amber-950/15 sm:px-6">
          <p className="text-xs font-semibold text-amber-800 dark:text-amber-200">{risks.map((issue) => riskShortLabel(issue.code, locale)).join(" · ")}</p>
        </div>
      ) : null}

      <div className="space-y-0 divide-y">
        <EditableMaxScore
          fieldKey={`${problem.q_id}:max-score`}
          problem={problem}
          readOnly={readOnly}
          saving={saving}
          locale={locale}
          onDirtyChange={onDirtyChange}
          onSave={onSaveMaxScore}
        />
        <EditableTextField fieldKey={`${problem.q_id}:stem`} label={tx(locale, "题目", "Question")} value={problem.stem} problem={problem} field="stem" readOnly={readOnly} saving={saving} locale={locale} onDirtyChange={onDirtyChange} onSave={onSaveText} />

        <div className="grid divide-y md:grid-cols-2 md:divide-x md:divide-y-0">
          <EditableTextField fieldKey={`${problem.q_id}:answer`} label={tx(locale, "标答 / 解题步骤", "Reference Answer / Solution Steps")} value={problem.reference_answer ?? ""} problem={problem} field="reference_answer" readOnly={readOnly} saving={saving} locale={locale} onDirtyChange={onDirtyChange} onSave={onSaveText} />
          <EditableTextField fieldKey={`${problem.q_id}:rubric`} label={tx(locale, "评分标准（与标答步骤对应）", "Rubric (Aligned with Reference Answer Steps)")} value={problem.criterion ?? ""} problem={problem} field="criterion" readOnly={readOnly} saving={saving} locale={locale} onDirtyChange={onDirtyChange} onSave={onSaveText} />
        </div>

        {programming ? (
          <section className="bg-slate-50/45 px-5 py-5 dark:bg-slate-950/15 sm:px-6">
            <h3 className="text-[15px] font-bold text-foreground">{tx(locale, "编程题校验", "Programming Validation")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{tx(locale, "按成熟 OJ 习惯查看公开样例、隐藏测试、输入、期望输出与解释。", "Review examples and hidden tests with explicit input, expected output, and explanation.")}</p>
            <div className="mt-4 overflow-hidden rounded-[9px] border bg-card">
              <EditableTextField compact fieldKey={`${problem.q_id}:code`} label={tx(locale, "参考代码", "Reference Solution")} value={problem.solution_code ?? ""} problem={problem} field="solution_code" readOnly={readOnly} saving={saving} locale={locale} onDirtyChange={onDirtyChange} onSave={onSaveText} />
              <TestCasesPanel fieldKey={`${problem.q_id}:tests`} problem={problem} readOnly={readOnly} saving={saving} locale={locale} onDirtyChange={onDirtyChange} onSave={onSaveTests} />
            </div>
          </section>
        ) : null}
      </div>

      <footer className="flex items-center justify-between border-t px-5 py-3 sm:px-6">
        <span className="text-xs text-muted-foreground">{tx(locale, "浏览态会渲染 LaTeX 或代码；点击修改可编辑源码。", "LaTeX and code are rendered while browsing; click Edit to edit the source.")}</span>
        <QuestionNavigator previous={previous} next={next} locale={locale} onNavigate={onNavigate} compact />
      </footer>
    </article>
  );
}

function EditableMaxScore({ fieldKey, problem, readOnly, saving, locale, onDirtyChange, onSave }: {
  fieldKey: string;
  problem: ProblemInfo;
  readOnly: boolean;
  saving: boolean;
  locale: string;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSave: (problem: ProblemInfo, maxScore: number) => Promise<void>;
}) {
  const original = String(problem.max_score ?? 10);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(original);
  const [error, setError] = useState<string | null>(null);
  const dirty = editing && draft !== original;

  useEffect(() => { if (!editing) setDraft(original); }, [editing, original]);
  useEffect(() => { onDirtyChange(fieldKey, dirty); return () => onDirtyChange(fieldKey, false); }, [dirty, fieldKey, onDirtyChange]);

  async function save() {
    const parsed = Number(draft);
    if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 10_000) {
      setError(tx(locale, "满分必须大于 0 且不超过 10000。", "The maximum score must be greater than 0 and no more than 10000."));
      return;
    }
    setError(null);
    try {
      await onSave(problem, parsed);
      onDirtyChange(fieldKey, false);
      setEditing(false);
    } catch {
      setError(tx(locale, "保存失败，请重试。", "Save failed. Try again."));
    }
  }

  const sourceLabel = maxScoreSourceLabel(problem.max_score_source, locale);
  const needsReview = problem.max_score_review_status !== "confirmed";
  return (
    <section className="bg-blue-50/35 px-5 py-4 dark:bg-blue-950/10 sm:px-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-bold text-foreground">{tx(locale, "本题满分", "Question Maximum Score")}</h3>
            <span className={cn("rounded-full px-2.5 py-1 text-xs font-semibold", needsReview ? "bg-amber-100 text-amber-700 dark:bg-amber-950/35 dark:text-amber-300" : "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/35 dark:text-emerald-300")}>{needsReview ? tx(locale, "请确认", "Confirm") : tx(locale, "已确认", "Confirmed")}</span>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{sourceLabel} · {tx(locale, "评分标准中的步骤按百分比分配到该满分。", "Rubric percentages are applied to this maximum score.")}</p>
        </div>
        {editing ? (
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative">
              <span className="sr-only">{tx(locale, `第 ${problem.number || problem.q_id} 题满分`, `Maximum score for question ${problem.number || problem.q_id}`)}</span>
              <input
                aria-label={tx(locale, `第 ${problem.number || problem.q_id} 题满分`, `Maximum score for question ${problem.number || problem.q_id}`)}
                type="number"
                inputMode="decimal"
                min="0.01"
                max="10000"
                step="0.5"
                value={draft}
                onChange={(event) => { setDraft(event.target.value); setError(null); }}
                className="h-9 w-32 rounded-[7px] border bg-background px-3 pr-9 text-sm font-semibold outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
              <span className="pointer-events-none absolute right-3 top-2.5 text-[11px] text-muted-foreground">{tx(locale, "分", "pts")}</span>
            </label>
            <button type="button" disabled={saving || (!dirty && !needsReview)} onClick={() => void save()} className="inline-flex h-9 items-center gap-1.5 rounded-[7px] bg-primary px-3 text-xs font-semibold text-primary-foreground disabled:opacity-45">{saving ? <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Save aria-hidden="true" className="h-3.5 w-3.5" />}{needsReview && !dirty ? tx(locale, "确认", "Confirm") : tx(locale, "保存", "Save")}</button>
            <button type="button" onClick={() => { setEditing(false); setDraft(original); setError(null); }} className="inline-flex h-9 items-center gap-1 rounded-[7px] border px-3 text-xs font-semibold hover:bg-muted"><X aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "取消", "Cancel")}</button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <strong className="text-lg text-foreground">{formatScore(problem.max_score ?? 10)} {tx(locale, "分", "pts")}</strong>
            {needsReview && !readOnly ? <button type="button" disabled={saving} onClick={() => void save()} className="inline-flex h-8 items-center gap-1.5 rounded-[6px] bg-primary px-2.5 text-xs font-semibold text-primary-foreground disabled:opacity-45"><Check aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "确认此满分", "Confirm score")}</button> : null}
            {!readOnly ? <button type="button" aria-label={tx(locale, `修改第 ${problem.number || problem.q_id} 题满分`, `Edit maximum score for question ${problem.number || problem.q_id}`)} onClick={() => setEditing(true)} className="inline-flex h-8 items-center gap-1.5 rounded-[6px] border px-2.5 text-xs font-semibold hover:bg-muted"><Pencil aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "修改", "Edit")}</button> : null}
          </div>
        )}
      </div>
      {error ? <p role="alert" className="mt-2 text-xs text-danger">{error}</p> : null}
    </section>
  );
}

function EditableTextField({ fieldKey, label, value, problem, field, readOnly, saving, locale, compact = false, onDirtyChange, onSave }: {
  fieldKey: string;
  label: string;
  value: string;
  problem: ProblemInfo;
  field: TextFieldKey;
  readOnly: boolean;
  saving: boolean;
  locale: string;
  compact?: boolean;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSave: (problem: ProblemInfo, field: TextFieldKey, value: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState(false);
  const dirty = editing && draft !== value;

  useEffect(() => { if (!editing) setDraft(value); }, [editing, value]);
  useEffect(() => { onDirtyChange(fieldKey, dirty); return () => onDirtyChange(fieldKey, false); }, [dirty, fieldKey, onDirtyChange]);

  async function save() {
    setError(false);
    try {
      await onSave(problem, field, draft);
      onDirtyChange(fieldKey, false);
      setEditing(false);
    } catch {
      setError(true);
    }
  }

  return (
    <section className={cn("min-w-0 px-5 py-5 sm:px-6", compact && "px-4 py-4 sm:px-5")}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-bold text-foreground">{label}</h3>
        {!readOnly ? <button type="button" aria-label={`${label} · ${editing ? tx(locale, "取消修改", "Cancel edit") : tx(locale, "修改", "Edit")}`} onClick={() => { setEditing((current) => !current); setError(false); }} className="inline-flex h-8 items-center gap-1.5 rounded-[6px] border px-2.5 text-xs font-semibold text-foreground hover:bg-muted">
          {editing ? <X aria-hidden="true" className="h-3.5 w-3.5" /> : <Pencil aria-hidden="true" className="h-3.5 w-3.5" />}
          {editing ? tx(locale, "取消", "Cancel") : tx(locale, "修改", "Edit")}
        </button> : null}
      </div>
      {editing ? (
        <div className="mt-3">
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={field === "stem" ? 7 : compact ? 6 : 8} className={cn("w-full resize-y rounded-[8px] border bg-background px-4 py-3 text-sm leading-6 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15", field === "solution_code" && "font-mono text-xs")} />
          <div className="mt-3 flex items-center justify-between gap-3">
            <p className={cn("text-xs", error ? "text-danger" : "text-muted-foreground")}>{error
              ? tx(locale, "保存失败，请重试。", "Save failed. Try again.")
              : field === "solution_code"
                ? tx(locale, "可直接编辑源码；保存后恢复语法高亮。", "Edit the source code directly; syntax highlighting returns after saving.")
                : tx(locale, "编辑态保留原始 Markdown / LaTeX。", "Raw Markdown / LaTeX is preserved while editing.")}</p>
            <button type="button" disabled={saving || !dirty} onClick={() => void save()} className="inline-flex h-9 items-center gap-2 rounded-[7px] bg-primary px-4 text-xs font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-45">{saving ? <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Save aria-hidden="true" className="h-3.5 w-3.5" />}{tx(locale, "保存", "Save")}</button>
          </div>
        </div>
      ) : (
        <div className={cn("mt-3 min-h-[92px] rounded-[8px] bg-slate-50 px-4 py-3 text-sm leading-6 dark:bg-slate-950/20", compact && "min-h-[72px]")}>
          {value.trim() ? (field === "solution_code"
            ? <SyntaxHighlightedCode code={value} languageHint={`${problem.type}\n${problem.stem}`} locale={locale} />
            : <MarkdownMath>{value}</MarkdownMath>) : <p className="text-sm text-muted-foreground">{tx(locale, "SmarTAI 正在生成或本次处理未成功，请在风险总览查看。", "SmarTAI generation is pending or failed; check the risk overview.")}</p>}
        </div>
      )}
    </section>
  );
}

function TestCasesPanel({ fieldKey, problem, readOnly, saving, locale, onDirtyChange, onSave }: {
  fieldKey: string;
  problem: ProblemInfo;
  readOnly: boolean;
  saving: boolean;
  locale: string;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSave: (problem: ProblemInfo, cases: TestCase[]) => Promise<void>;
}) {
  const original = problem.test_cases ?? EMPTY_TEST_CASES;
  const [editing, setEditing] = useState(false);
  const [cases, setCases] = useState<TestCase[]>(original);
  const [activeIndex, setActiveIndex] = useState(0);
  const [error, setError] = useState(false);
  const dirty = editing && JSON.stringify(cases) !== JSON.stringify(original);
  useEffect(() => { if (!editing) setCases(original); }, [editing, original]);
  useEffect(() => { onDirtyChange(fieldKey, dirty); return () => onDirtyChange(fieldKey, false); }, [dirty, fieldKey, onDirtyChange]);
  const exampleCount = original.filter((item) => (item.visibility ?? "example") === "example").length;
  const hiddenCount = original.length - exampleCount;

  async function save() {
    setError(false);
    try {
      await onSave(problem, cases);
      onDirtyChange(fieldKey, false);
      setEditing(false);
    } catch {
      setError(true);
    }
  }

  return (
    <section className="border-t px-4 py-4 sm:px-5">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h4 className="text-sm font-bold text-foreground">{tx(locale, "测试样例", "Test Cases")}</h4>
          <p className="mt-1 text-xs text-muted-foreground">{tx(locale, `${exampleCount} 个公开样例 · ${hiddenCount} 个隐藏测试`, `${exampleCount} examples · ${hiddenCount} hidden tests`)}</p>
        </div>
        {!readOnly ? <button type="button" onClick={() => { setEditing((current) => !current); setError(false); }} className="inline-flex h-8 items-center gap-1.5 rounded-[6px] border px-2.5 text-xs font-semibold hover:bg-muted">{editing ? <X aria-hidden="true" className="h-3.5 w-3.5" /> : <Pencil aria-hidden="true" className="h-3.5 w-3.5" />}{editing ? tx(locale, "取消", "Cancel") : tx(locale, "修改", "Edit")}</button> : null}
      </div>

      {editing ? (
        <div className="mt-4 space-y-3">
          {cases.map((testCase, index) => (
            <article key={index} className="rounded-[8px] border bg-slate-50 p-4 dark:bg-slate-950/20">
              <div className="flex items-center justify-between gap-3">
                <input value={testCase.title ?? ""} onChange={(event) => setCases(updateCase(cases, index, { title: event.target.value }))} placeholder={tx(locale, `样例 ${index + 1}`, `Example ${index + 1}`)} className="h-9 min-w-0 flex-1 rounded-[6px] border bg-card px-3 text-sm font-semibold outline-none focus:border-primary" />
                <select value={testCase.visibility ?? "example"} onChange={(event) => setCases(updateCase(cases, index, { visibility: event.target.value as "example" | "hidden" }))} className="h-9 rounded-[6px] border bg-card px-2 text-xs outline-none focus:border-primary"><option value="example">{tx(locale, "公开样例", "Example")}</option><option value="hidden">{tx(locale, "隐藏测试", "Hidden")}</option></select>
                <button type="button" onClick={() => setCases(cases.filter((_, itemIndex) => itemIndex !== index))} className="inline-flex h-9 w-9 items-center justify-center rounded-[6px] text-muted-foreground hover:bg-red-50 hover:text-danger"><Trash2 aria-hidden="true" className="h-4 w-4" /></button>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <CaseTextarea label={tx(locale, "输入", "Input")} value={testCase.input} onChange={(value) => setCases(updateCase(cases, index, { input: value }))} />
                <CaseTextarea label={tx(locale, "期望输出", "Expected Output")} value={testCase.expected_output} onChange={(value) => setCases(updateCase(cases, index, { expected_output: value }))} />
              </div>
              <div className="mt-3"><CaseTextarea label={tx(locale, "解释（可选）", "Explanation (optional)")} value={testCase.description} rows={2} onChange={(value) => setCases(updateCase(cases, index, { description: value }))} /></div>
            </article>
          ))}
          <button type="button" onClick={() => setCases([...cases, emptyTestCase(cases.length + 1)])} className="inline-flex h-9 items-center gap-2 rounded-[7px] border px-3 text-xs font-semibold hover:bg-muted"><Plus aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "添加测试样例", "Add Test Case")}</button>
          <div className="flex items-center justify-between gap-3 border-t pt-3">
            <p className={cn("text-xs", error ? "text-danger" : "text-muted-foreground")}>{error ? tx(locale, "保存失败，请重试。", "Save failed. Try again.") : tx(locale, "隐藏测试只对教师可见。", "Hidden tests are visible only to teachers.")}</p>
            <button type="button" disabled={saving || !dirty} onClick={() => void save()} className="inline-flex h-9 items-center gap-2 rounded-[7px] bg-primary px-4 text-xs font-semibold text-primary-foreground disabled:opacity-45">{saving ? <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Save aria-hidden="true" className="h-3.5 w-3.5" />}{tx(locale, "保存测试样例", "Save Test Cases")}</button>
          </div>
        </div>
      ) : original.length ? (
        <div className="mt-4">
          <div className="flex gap-2 overflow-x-auto pb-2">
            {original.map((testCase, index) => (
              <button key={index} type="button" onClick={() => setActiveIndex(index)} className={cn("inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[6px] border px-3 text-xs font-semibold", activeIndex === index ? "border-primary bg-blue-50 text-primary dark:bg-blue-950/20" : "text-muted-foreground hover:bg-muted")}>{(testCase.visibility ?? "example") === "hidden" ? <EyeOff aria-hidden="true" className="h-3.5 w-3.5" /> : <Eye aria-hidden="true" className="h-3.5 w-3.5" />}{testCase.title || tx(locale, `样例 ${index + 1}`, `Example ${index + 1}`)}</button>
            ))}
          </div>
          <TestCaseViewer testCase={original[Math.min(activeIndex, original.length - 1)]} locale={locale} />
        </div>
      ) : <p className="mt-4 rounded-[8px] bg-slate-50 px-4 py-5 text-sm text-muted-foreground dark:bg-slate-950/20">{tx(locale, "测试样例生成失败或尚未完成，请在风险总览查看。", "Test case generation failed or is incomplete; check the risk overview.")}</p>}
    </section>
  );
}

function TestCaseViewer({ testCase, locale }: { testCase: TestCase; locale: string }) {
  const functionMode = Boolean(testCase.function_name || testCase.io_mode === "function");
  const input = functionMode ? JSON.stringify(testCase.function_args ?? [], null, 2) : testCase.input;
  const output = functionMode ? (testCase.expected_return ?? "") : testCase.expected_output;
  return (
    <article className="rounded-[8px] border bg-slate-50 p-4 dark:bg-slate-950/20">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className={cn("rounded-full px-2.5 py-1 font-semibold", (testCase.visibility ?? "example") === "hidden" ? "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-200" : "bg-blue-100 text-blue-700")}>{(testCase.visibility ?? "example") === "hidden" ? tx(locale, "隐藏测试", "Hidden") : tx(locale, "公开样例", "Example")}</span>
        <span className="text-muted-foreground">{functionMode ? tx(locale, "函数调用", "Function Call") : "stdin / stdout"}</span>
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <CodeBlock label={tx(locale, "输入", "Input")} value={input} />
        <CodeBlock label={tx(locale, "期望输出", "Expected Output")} value={output} />
      </div>
      {testCase.description ? <div className="mt-4"><p className="text-xs font-semibold text-muted-foreground">{tx(locale, "解释", "Explanation")}</p><p className="mt-1 text-sm leading-6 text-foreground">{testCase.description}</p></div> : null}
    </article>
  );
}

function CodeBlock({ label, value }: { label: string; value: string }) {
  return <div><p className="text-xs font-semibold text-muted-foreground">{label}</p><pre className="mt-2 min-h-[72px] overflow-x-auto rounded-[7px] bg-slate-900 px-4 py-3 font-mono text-xs leading-5 text-slate-100">{value || "—"}</pre></div>;
}

function CaseTextarea({ label, value, rows = 4, onChange }: { label: string; value: string; rows?: number; onChange: (value: string) => void }) {
  return <label className="grid gap-1.5 text-xs font-semibold text-muted-foreground">{label}<textarea value={value} rows={rows} onChange={(event) => onChange(event.target.value)} className="w-full resize-y rounded-[6px] border bg-card px-3 py-2 font-mono text-xs leading-5 text-foreground outline-none focus:border-primary" /></label>;
}

function QuestionNavigator({ previous, next, locale, onNavigate, compact = false }: { previous: ProblemInfo | null; next: ProblemInfo | null; locale: string; onNavigate: (qId: string) => void; compact?: boolean }) {
  return (
    <div className="flex gap-2">
      <button type="button" disabled={!previous} onClick={() => previous && onNavigate(previous.q_id)} className={cn("inline-flex items-center justify-center gap-1.5 rounded-[6px] border text-xs font-semibold hover:bg-muted disabled:opacity-30", compact ? "h-8 w-8 px-0" : "h-9 px-3")} aria-label={tx(locale, "上一题", "Previous question")}><ArrowUp aria-hidden="true" className="h-3.5 w-3.5" />{compact ? null : tx(locale, "上一题", "Previous")}</button>
      <button type="button" disabled={!next} onClick={() => next && onNavigate(next.q_id)} className={cn("inline-flex items-center justify-center gap-1.5 rounded-[6px] border text-xs font-semibold hover:bg-muted disabled:opacity-30", compact ? "h-8 w-8 px-0" : "h-9 px-3")} aria-label={tx(locale, "下一题", "Next question")}>{compact ? null : tx(locale, "下一题", "Next")}<ArrowDown aria-hidden="true" className="h-3.5 w-3.5" /></button>
    </div>
  );
}

function isKeyboardNavigationBlocked(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable='true'], [role='dialog']"));
}

function updateCase(cases: TestCase[], index: number, patch: Partial<TestCase>) {
  return cases.map((testCase, itemIndex) => itemIndex === index ? { ...testCase, ...patch } : testCase);
}

function emptyTestCase(index: number): TestCase {
  return { title: `样例 ${index}`, visibility: "example", purpose: "normal", io_mode: "stdin", input: "", expected_output: "", description: "", source: "teacher", sandbox_feasible: true };
}

function filterProblems(problems: ProblemInfo[], rawQuery: string) {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) return problems;
  const tokens = query.split(/[\s,，;；]+/).filter(Boolean);
  return problems.filter((problem) => tokens.every((token) => {
    if (["编程", "编程题", "programming"].includes(token)) return isProgrammingProblem(problem);
    if (["低置信", "low-confidence"].includes(token)) return (problem.preparation_issues ?? []).some((issue) => issue.status === "open" && issue.code === "low_confidence");
    if (["冲突", "conflict"].includes(token)) return (problem.preparation_issues ?? []).some((issue) => issue.status === "open" && issue.code.includes("conflict"));
    const sourceText = [problem.number, problem.q_id, problem.type, problem.max_score, problem.max_score_source, problem.stem, problem.reference_answer, problem.criterion].join(" ");
    return `${sourceText} ${questionSearchAliases(sourceText)}`.toLocaleLowerCase().includes(token);
  }));
}

function sortProblems(problems: ProblemInfo[], locale: string) {
  return [...problems].sort((a, b) => (a.number || a.q_id).localeCompare(b.number || b.q_id, locale, { numeric: true }));
}

function openRiskCount(problem: ProblemInfo) {
  return (problem.preparation_issues ?? []).filter((issue) => issue.status === "open").length;
}

function riskShortLabel(code: string, locale: string) {
  const zh: Record<string, string> = { low_confidence: "低置信匹配", source_conflict: "来源冲突", ai_source_conflict: "SmarTAI 与原文件冲突", parse_anomaly: "解析异常", generation_failed: "生成失败", invalid_test_case: "测试样例无效", reference_solution_failed_case: "参考解未通过测试", rubric_step_reference_conflict: "评分步骤未对应", default_max_score_requires_review: "默认 10 分待确认", max_score_not_found: "未匹配到本题满分，暂按 10 分" };
  const en: Record<string, string> = { low_confidence: "Low confidence", source_conflict: "Source conflict", ai_source_conflict: "SmarTAI/source conflict", parse_anomaly: "Parse anomaly", generation_failed: "Generation failed", invalid_test_case: "Invalid test case", reference_solution_failed_case: "Reference solution failed", rubric_step_reference_conflict: "Rubric alignment issue", default_max_score_requires_review: "Default 10-point score needs confirmation", max_score_not_found: "No matched score; temporarily 10" };
  return locale === "zh-CN" ? zh[code] ?? code : en[code] ?? code;
}

function maxScoreSourceLabel(source: ProblemInfo["max_score_source"], locale: string) {
  const labels = {
    default_10: ["系统暂按默认 10 分", "System default of 10 points"],
    uniform: ["来自统一满分设置", "From the uniform score setting"],
    per_question_text: ["来自每题分值说明的识别结果", "Interpreted from the per-question score note"],
    teacher_edited: ["已由教师手动修改", "Manually edited by the teacher"],
    legacy: ["来自历史题目数据", "From legacy question data"],
  } as const;
  const value = labels[source ?? "legacy"];
  return locale === "zh-CN" ? value[0] : value[1];
}

function formatScore(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function questionAnchorId(qId: string) {
  return `question-${qId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function scrollQuestionIntoView(qId: string, behavior: ScrollBehavior) {
  const element = document.getElementById(questionAnchorId(qId));
  if (!element) return;
  const stickyHeaderOffset = 86;
  const top = window.scrollY + element.getBoundingClientRect().top - stickyHeaderOffset;
  window.scrollTo({ top: Math.max(0, top), left: 0, behavior });
}

function tx(locale: string, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

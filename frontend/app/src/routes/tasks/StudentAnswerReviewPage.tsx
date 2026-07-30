import {
  AlertCircle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Check,
  CheckCircle2,
  ChevronRight,
  FileText,
  Keyboard,
  LoaderCircle,
  Pencil,
  RotateCcw,
  Save,
  Search,
  X,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
} from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { useTask, useUpdateStudentAnswer, useUpdateStudentIdentity } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { Button } from "@/components/ui/Button";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { questionSearchAliases } from "@/lib/questionSearch";
import { isWorkflowRevisionConflictCode } from "@/lib/taskActionGuards";
import {
  answerMap,
  buildSubmissionQuestions,
  getAnswerState,
  type SubmissionAnswerState,
  type SubmissionQuestion,
} from "@/lib/submissionReview";
import { getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { StudentAnswerInfo, StudentSubmission } from "@/types";

type PickerItem = {
  id: string;
  primary: string;
  secondary: string;
  searchable: string;
  exactValues: string[];
  attention?: boolean;
};

type PickerMatch = {
  item: PickerItem;
  kind: "all" | "exact" | "related";
};

type AnswerDraft = {
  content: string;
};

const STATUS_KEYS: Record<SubmissionAnswerState, MessageKey> = {
  recognized: "studentSubmissionStatusRecognized",
  reviewed: "studentSubmissionStatusReviewed",
  flagged: "studentSubmissionStatusFlagged",
  empty: "studentSubmissionStatusEmpty",
  missing: "studentSubmissionStatusMissing",
};

/** Merged S04/S05: one selected student with all questions in one continuous review. */
export function StudentAnswerReviewPage() {
  const { taskId, studentId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const { locale, t } = useI18n();
  const taskQuery = useTask(taskId);
  const readOnly = Boolean(taskQuery.data && taskQuery.data.status !== "submissions_ready");
  const answerMutation = useUpdateStudentAnswer();
  const identityMutation = useUpdateStudentIdentity();
  const [drafts, setDrafts] = useState<Record<string, AnswerDraft>>({});
  const [editingQuestionIds, setEditingQuestionIds] = useState<Set<string>>(new Set());
  const [savingQuestionId, setSavingQuestionId] = useState<string | null>(null);
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const requestedQuestionId = searchParams.get("question") ?? "";
  const [activeQuestionId, setActiveQuestionId] = useState(requestedQuestionId);
  const [identityOpen, setIdentityOpen] = useState(false);
  const [identityId, setIdentityId] = useState("");
  const [identityName, setIdentityName] = useState("");
  const [identityError, setIdentityError] = useState<string | null>(null);
  const questionFilterParam = searchParams.get("questionFilter") ?? "";
  const initializedStudentRef = useRef<string | null>(null);
  const positionedRouteRef = useRef<string | null>(null);
  const selectedQuestionRef = useRef(requestedQuestionId);
  const resetScrollForStudentRef = useRef<string | null>(null);

  const students = useMemo(
    () => Object.values(taskQuery.data?.student_data ?? {}).sort(compareStudents),
    [taskQuery.data?.student_data],
  );
  const questions = useMemo(
    () => buildSubmissionQuestions(Object.values(taskQuery.data?.problem_data ?? {}), students),
    [students, taskQuery.data?.problem_data],
  );
  const student = useMemo(
    () => students.find((candidate) => candidate.stu_id === studentId),
    [studentId, students],
  );
  const answers = useMemo(() => student ? answerMap(student) : new Map(), [student]);
  const questionItems = useMemo(() => questions.map(questionPickerItem), [questions]);
  const questionMatches = useMemo(
    () => matchPickerItems(questionItems, questionFilterParam),
    [questionFilterParam, questionItems],
  );
  const filteredQuestions = useMemo(
    () => selectMatched(questions, questionMatches, questionFilterParam, (value) => value.id),
    [questionFilterParam, questionMatches, questions],
  );
  const studentNeighbors = neighbors(students, studentId, (value) => value.stu_id);
  const activeIndex = Math.max(0, filteredQuestions.findIndex((question) => question.id === activeQuestionId));
  const activeQuestion = filteredQuestions[activeIndex] ?? filteredQuestions[0] ?? null;
  const questionNeighbors = {
    previous: activeIndex > 0 ? filteredQuestions[activeIndex - 1] : null,
    next: activeIndex >= 0 && activeIndex < filteredQuestions.length - 1 ? filteredQuestions[activeIndex + 1] : null,
  };

  useEffect(() => {
    if (requestedQuestionId) selectedQuestionRef.current = requestedQuestionId;
  }, [requestedQuestionId]);

  useEffect(() => {
    setIdentityOpen(false);
    setIdentityId(student?.stu_id ?? "");
    setIdentityName(student?.stu_name ?? "");
    setIdentityError(null);
    setEditingQuestionIds(new Set());
  }, [student?.stu_id, student?.stu_name]);

  useEffect(() => {
    if (!student) return;
    if (initializedStudentRef.current === student.stu_id) {
      setDrafts((current) => {
        let changed = false;
        const next = { ...current };
        questions.forEach((question) => {
          if (next[question.id]) return;
          const answer = answers.get(question.id);
          next[question.id] = { content: answer?.content ?? "" };
          changed = true;
        });
        return changed ? next : current;
      });
      return;
    }
    initializedStudentRef.current = student.stu_id;
    setDrafts(Object.fromEntries(questions.map((question) => {
      const answer = answers.get(question.id);
      return [question.id, { content: answer?.content ?? "" }];
    })));
    setSaveErrors({});
  }, [answers, questions, student]);

  const dirtyQuestionIds = useMemo(() => new Set(questions.flatMap((question) => {
    const answer = answers.get(question.id);
    const draft = drafts[question.id];
    if (!draft) return [];
    return draft.content !== (answer?.content ?? "")
      ? [question.id]
      : [];
  })), [answers, drafts, questions]);
  const isDirty = dirtyQuestionIds.size > 0;

  useEffect(() => {
    if (!filteredQuestions.length) return;
    const requested = filteredQuestions.find((question) => question.id === requestedQuestionId) ?? filteredQuestions[0];
    const routeKey = `${studentId ?? ""}:${requestedQuestionId}:${questionFilterParam}`;
    if (positionedRouteRef.current === routeKey) return;
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        positionedRouteRef.current = routeKey;
        setActiveQuestionId(requested.id);
        if (resetScrollForStudentRef.current === studentId) {
          resetScrollForStudentRef.current = null;
          window.scrollTo({ top: 0, left: 0, behavior: "auto" });
        } else if (requestedQuestionId && requested.id === requestedQuestionId) scrollQuestionIntoView(requested.id, "auto");
        else window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [filteredQuestions, questionFilterParam, requestedQuestionId, studentId]);

  useEffect(() => {
    if (!filteredQuestions.length) return;
    let frame = 0;
    const updateActiveQuestion = () => {
      frame = 0;
      let active = filteredQuestions[0]?.id ?? "";
      const scrollRoot = document.scrollingElement ?? document.documentElement;
      const reachedDocumentEnd = scrollRoot.scrollHeight > window.innerHeight + 1
        && window.scrollY + window.innerHeight >= scrollRoot.scrollHeight - 2;
      if (reachedDocumentEnd) {
        active = filteredQuestions[filteredQuestions.length - 1]?.id ?? active;
      } else {
        for (const question of filteredQuestions) {
          const element = document.getElementById(questionAnchorId(question.id));
          if (!element || element.getBoundingClientRect().top > 112) break;
          active = question.id;
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
  }, [filteredQuestions]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (isKeyboardNavigationBlocked(event.target)) return;
      if (event.key === "ArrowLeft" && studentNeighbors.previous) {
        event.preventDefault();
        goToStudent(studentNeighbors.previous);
      } else if (event.key === "ArrowRight" && studentNeighbors.next) {
        event.preventDefault();
        goToStudent(studentNeighbors.next);
      } else if (event.key === "ArrowUp" && questionNeighbors.previous) {
        event.preventDefault();
        scrollToQuestion(questionNeighbors.previous.id);
      } else if (event.key === "ArrowDown" && questionNeighbors.next) {
        event.preventDefault();
        scrollToQuestion(questionNeighbors.next.id);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // Navigation callbacks intentionally use the latest route and dirty state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDirty, questionNeighbors.next?.id, questionNeighbors.previous?.id, studentNeighbors.next?.stu_id, studentNeighbors.previous?.stu_id]);

  useEffect(() => {
    if (!isDirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [isDirty]);

  if (taskQuery.isSuccess && taskId && !hasTaskReachedStep(taskQuery.data, 4)) {
    return <Navigate replace to={getTaskDestination(taskQuery.data)} />;
  }

  function setFilterParam(key: "questionFilter", value: string) {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      next.delete("studentFilter");
      if (value.trim()) next.set(key, value);
      else next.delete(key);
      return next;
    }, { replace: true });
  }

  function confirmLeave() {
    return !isDirty || window.confirm(t("answerReviewUnsavedConfirm"));
  }

  function goToStudent(target: StudentSubmission | null) {
    const selectedQuestionStillVisible = filteredQuestions.some((question) => question.id === selectedQuestionRef.current);
    const anchorQuestionId = selectedQuestionStillVisible
      ? selectedQuestionRef.current
      : activeQuestion?.id || requestedQuestionId;
    if (!target || !taskId || !anchorQuestionId || !confirmLeave()) return;
    const nextSearch = new URLSearchParams(searchParams);
    nextSearch.delete("studentFilter");
    nextSearch.set("question", anchorQuestionId);
    nextSearch.delete("from");
    resetScrollForStudentRef.current = target.stu_id;
    navigate({
      pathname: studentPath(taskId, target.stu_id),
      search: `?${nextSearch.toString()}`,
    });
  }

  function scrollToQuestion(targetId: string, behavior: ScrollBehavior = "smooth") {
    selectedQuestionRef.current = targetId;
    setActiveQuestionId(targetId);
    scrollQuestionIntoView(targetId, behavior);
  }

  function updateDraft(qId: string, patch: Partial<AnswerDraft>) {
    setDrafts((current) => ({
      ...current,
      [qId]: { ...(current[qId] ?? { content: "" }), ...patch },
    }));
    setSaveErrors((current) => {
      if (!current[qId]) return current;
      const next = { ...current };
      delete next[qId];
      return next;
    });
  }

  async function saveAnswer(question: SubmissionQuestion, moveNext: boolean) {
    if (!taskId || !student || !taskQuery.data || readOnly) return;
    const draft = drafts[question.id];
    if (!draft) return;
    setSavingQuestionId(question.id);
    setSaveErrors((current) => {
      const next = { ...current };
      delete next[question.id];
      return next;
    });
    try {
      await answerMutation.mutateAsync({
        taskId,
        studentId: student.stu_id,
        qId: question.id,
        expectedWorkflowRevision: taskQuery.data.workflow_revision,
        content: draft.content,
      });
      setEditingQuestionIds((current) => {
        const nextIds = new Set(current);
        nextIds.delete(question.id);
        return nextIds;
      });
      toast.success(t("answerReviewSaved"));
      if (moveNext) {
        const index = filteredQuestions.findIndex((candidate) => candidate.id === question.id);
        const next = index >= 0 ? filteredQuestions[index + 1] : null;
        if (next) window.requestAnimationFrame(() => scrollToQuestion(next.id));
      }
    } catch (error) {
      setSaveErrors((current) => ({ ...current, [question.id]: answerErrorMessage(error, t) }));
    } finally {
      setSavingQuestionId(null);
    }
  }

  async function confirmAnswer(question: SubmissionQuestion) {
    if (!taskId || !student || !taskQuery.data || readOnly) return;
    setSavingQuestionId(question.id);
    setSaveErrors((current) => {
      const next = { ...current };
      delete next[question.id];
      return next;
    });
    try {
      await answerMutation.mutateAsync({
        taskId,
        studentId: student.stu_id,
        qId: question.id,
        expectedWorkflowRevision: taskQuery.data.workflow_revision,
        reviewStatus: "confirmed",
      });
      toast.success(t("answerReviewConfirmed"));
    } catch (error) {
      setSaveErrors((current) => ({ ...current, [question.id]: answerErrorMessage(error, t) }));
    } finally {
      setSavingQuestionId(null);
    }
  }

  function startEditing(question: SubmissionQuestion) {
    if (readOnly) return;
    const answer = answers.get(question.id);
    setDrafts((current) => ({
      ...current,
      [question.id]: { content: answer?.content ?? "" },
    }));
    setEditingQuestionIds((current) => new Set(current).add(question.id));
  }

  function cancelEditing(question: SubmissionQuestion) {
    const answer = answers.get(question.id);
    setDrafts((current) => ({
      ...current,
      [question.id]: { content: answer?.content ?? "" },
    }));
    setEditingQuestionIds((current) => {
      const nextIds = new Set(current);
      nextIds.delete(question.id);
      return nextIds;
    });
    setSaveErrors((current) => {
      if (!current[question.id]) return current;
      const nextErrors = { ...current };
      delete nextErrors[question.id];
      return nextErrors;
    });
  }

  async function saveIdentity() {
    if (!taskId || !student || !taskQuery.data || readOnly) return;
    const nextId = identityId.trim();
    const nextName = identityName.trim();
    if (!nextId || !nextName) {
      setIdentityError(t("studentSubmissionIdentityRequired"));
      return;
    }
    setIdentityError(null);
    try {
      const result = await identityMutation.mutateAsync({
        taskId,
        currentStudentId: student.stu_id,
        expectedWorkflowRevision: taskQuery.data.workflow_revision,
        studentId: nextId,
        studentName: nextName,
      });
      setIdentityOpen(false);
      toast.success(t("studentSubmissionIdentitySaved"));
      if (result.student.stu_id !== student.stu_id) {
        resetScrollForStudentRef.current = result.student.stu_id;
        navigate({
          pathname: studentPath(taskId, result.student.stu_id),
          search: searchParams.toString() ? `?${searchParams.toString()}` : "",
        }, { replace: true });
      }
    } catch (error) {
      setIdentityError(identityErrorMessage(error, t));
    }
  }

  const backHref = buildBackHref(taskId, searchParams);

  return (
    <div className="w-full max-w-[1300px]">
      <div className="flex min-h-9 items-center justify-between gap-4">
        <h1 className="min-w-0 truncate text-[28px] font-bold leading-9 tracking-[-0.02em] text-foreground sm:text-[30px]">
          {t("answerReviewTitle")}
        </h1>
        <Link
          to={backHref}
          onClick={(event) => { if (!confirmLeave()) event.preventDefault(); }}
          className="inline-flex shrink-0 items-center gap-1.5 text-[13px] font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          {t("answerReviewBackMatrix")}
        </Link>
      </div>
      <NewTaskStepper currentStep={4} />

      {readOnly ? (
        <div className="mt-4 rounded-[9px] border bg-card px-4 py-3 text-[12px] leading-5 text-muted-foreground">
          {tx(locale, "当前为已进入后续阶段的历史回看；学生身份与识别作答保持只读。", "This task has moved to a later stage. Student identity and recognized responses are read-only here.")}
        </div>
      ) : null}

      {taskQuery.isLoading ? (
        <PageState title={t("answerReviewLoading")} busy />
      ) : taskQuery.isError ? (
        <PageState title={t("answerReviewLoadError")} action={t("answerReviewRetry")} onAction={() => void taskQuery.refetch()} />
      ) : !taskId || !studentId ? (
        <PageState title={t("answerReviewRouteMissing")} href="/history" action={t("studentSubmissionBackHistory")} />
      ) : !student ? (
        <PageState
          title={t("answerReviewNotFound")}
          description={t("answerReviewNotFoundDescription")}
          href={`/tasks/${encodeURIComponent(taskId)}/submissions`}
          action={t("answerReviewBackMatrix")}
        />
      ) : (
        <section className="mt-[22px] min-w-0" aria-label={`${student.stu_id} · ${t("studentSubmissionAllAnswersTitle")}`}>
          <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4 lg:gap-5">
            <ReviewMetric
              label={t("studentSubmissionMetricIdentity")}
              value={t(student.identity_status === "needs_review" ? "studentSubmissionIdentityReview" : "studentSubmissionIdentityMatched")}
              detail={student.identity_match_method ? t(identityMethodKey(student.identity_match_method)) : "—"}
              tone={student.identity_status === "needs_review" ? "warning" : "accent"}
            />
            <ReviewMetric
              label={t("studentSubmissionMetricCoverage")}
              value={formatPercent(questions.filter((question) => Boolean(answers.get(question.id)?.content?.trim())).length, questions.length)}
              detail={`${questions.filter((question) => Boolean(answers.get(question.id)?.content?.trim())).length}/${questions.length}`}
            />
            <ReviewMetric
              label={t("studentSubmissionMetricReview")}
              value={String(questions.filter((question) => !["recognized", "reviewed"].includes(getAnswerState(answers.get(question.id)))).length)}
              detail={t("studentSubmissionMetricQuestions")}
              tone={questions.some((question) => !["recognized", "reviewed"].includes(getAnswerState(answers.get(question.id)))) ? "danger" : "accent"}
            />
            <ReviewMetric
              label={t("studentSubmissionMetricSource")}
              value={student.source_filename ? "1" : "—"}
              detail={student.source_filename ? t("studentSubmissionMetricFile") : t("studentSubmissionMetricUnknown")}
              tone="neutral"
            />
          </dl>

          <div className="mt-5">
          <StudentNavigation
            student={student}
            previous={studentNeighbors.previous}
            next={studentNeighbors.next}
            onPrevious={() => goToStudent(studentNeighbors.previous)}
            onNext={() => goToStudent(studentNeighbors.next)}
            identityOpen={identityOpen}
            readOnly={readOnly}
            onToggleIdentity={() => {
              if (readOnly) return;
              setIdentityOpen((open) => !open);
              setIdentityError(null);
            }}
            t={t}
          />
          </div>

          {identityOpen ? (
            <form
              className="mt-3 rounded-[10px] border bg-card p-4"
              onSubmit={(event) => {
                event.preventDefault();
                void saveIdentity();
              }}
            >
              <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-5">
                <div>
                  <h3 className="text-sm font-semibold text-foreground">{t("studentSubmissionIdentityTitle")}</h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("studentSubmissionIdentityDescription")}</p>
                </div>
                {student.source_filename ? (
                  <span className="inline-flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground" title={student.source_filename}>
                    <FileText aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                    <span className="max-w-[280px] truncate">{student.source_filename}</span>
                  </span>
                ) : null}
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
                <label className="grid gap-1.5 text-xs font-medium text-foreground">
                  {t("studentSubmissionStudentId")}
                  <input value={identityId} maxLength={160} onChange={(event) => setIdentityId(event.target.value)} className="h-10 rounded-[7px] border bg-card px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
                </label>
                <label className="grid gap-1.5 text-xs font-medium text-foreground">
                  {t("studentSubmissionStudentName")}
                  <input value={identityName} maxLength={160} onChange={(event) => setIdentityName(event.target.value)} className="h-10 rounded-[7px] border bg-card px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
                </label>
                <Button type="submit" className="h-10 px-5" disabled={identityMutation.isPending}>
                  {identityMutation.isPending ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Check aria-hidden="true" className="h-4 w-4" />}
                  {t(identityMutation.isPending ? "studentSubmissionIdentitySaving" : "studentSubmissionIdentitySave")}
                </Button>
              </div>
              {identityError ? <p className="mt-2 text-xs font-medium text-danger" role="alert">{identityError}</p> : null}
            </form>
          ) : null}

          <div className="mt-3">
            <div className="rounded-[10px] border bg-card p-2">
              <SmartPicker
                label={t("answerReviewQuestionSearchLabel")}
                placeholder={t("answerReviewQuestionSearchPlaceholder")}
                query={questionFilterParam}
                matches={questionMatches}
                currentId={activeQuestion?.id ?? ""}
                onCommit={(value) => setFilterParam("questionFilter", value)}
                onSelect={(id) => scrollToQuestion(id)}
                t={t}
              />
            </div>
            <div className="mt-1.5 grid gap-0.5 px-1 text-[11px] leading-5 text-muted-foreground">
              <span>{tx(locale, "搜索只筛选题目；当前学生保持不变。输入中文时会在选词完成后再应用筛选。", "Question search only filters questions; the selected student stays unchanged. IME text is applied after composition finishes.")}</span>
              <span className="flex items-center gap-1.5 font-medium text-foreground/70">
                <Keyboard aria-hidden="true" className="h-3.5 w-3.5" />
                {t("answerReviewKeyboardHint")}
              </span>
            </div>
          </div>

          {filteredQuestions.length ? (
            <div className="mt-4 grid items-start gap-4 lg:grid-cols-[132px_minmax(0,1fr)]">
              <aside className="sticky top-[86px] z-20 hidden max-h-[calc(100vh-102px)] overflow-hidden rounded-[10px] border bg-card lg:flex lg:flex-col" aria-label={tx(locale, "题目导航", "Question navigation")}>
                <div className="shrink-0 border-b px-3 py-3">
                  <p className="text-xs font-bold text-foreground">{tx(locale, "题目导航", "Questions")}</p>
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{tx(locale, `共 ${filteredQuestions.length} 题 · 点击定位`, `${filteredQuestions.length} questions · select to locate`)}</p>
                </div>
                <div className="min-h-0 overflow-y-auto p-2 overscroll-contain">
                  {filteredQuestions.map((question) => {
                    const answer = answers.get(question.id);
                    const active = question.id === activeQuestion?.id;
                    const state = getAnswerState(answer);
                    return (
                      <button
                        key={question.id}
                        type="button"
                        aria-current={active ? "true" : undefined}
                        onClick={() => scrollToQuestion(question.id)}
                        className={cn(
                          "mb-1 flex min-h-10 w-full items-center justify-between rounded-[7px] px-2.5 text-left text-xs font-semibold transition last:mb-0",
                          active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                        )}
                      >
                        <span className="truncate">{tx(locale, `第 ${question.label} 题`, `Q${question.label}`)}</span>
                        <span className={cn(
                          "ml-1 h-2 w-2 shrink-0 rounded-full",
                          active ? "bg-white" : state === "recognized" ? "bg-emerald-500" : state === "reviewed" ? "bg-blue-500" : state === "flagged" ? "bg-amber-500" : "bg-red-500",
                        )} />
                      </button>
                    );
                  })}
                </div>
              </aside>

              <main className="min-w-0 space-y-5">
                {filteredQuestions.map((question, index) => (
                  <AnswerReviewCard
                    key={question.id}
                    question={question}
                    answer={answers.get(question.id)}
                    draft={drafts[question.id] ?? { content: "" }}
                    sourceFilename={student.source_filename}
                    previous={filteredQuestions[index - 1] ?? null}
                    next={filteredQuestions[index + 1] ?? null}
                    dirty={dirtyQuestionIds.has(question.id)}
                    saving={savingQuestionId === question.id}
                    saveError={saveErrors[question.id]}
                    editing={editingQuestionIds.has(question.id)}
                    readOnly={readOnly}
                    onDraftChange={(patch) => updateDraft(question.id, patch)}
                    onSave={(moveNext) => void saveAnswer(question, moveNext)}
                    onEdit={() => startEditing(question)}
                    onCancel={() => cancelEditing(question)}
                    onConfirm={() => void confirmAnswer(question)}
                    onNavigate={scrollToQuestion}
                    locale={locale}
                    t={t}
                  />
                ))}
              </main>
            </div>
          ) : (
            <div className="mt-4 flex min-h-[260px] items-center justify-center rounded-[10px] border bg-card px-6 text-center">
              <div>
                <AlertCircle aria-hidden="true" className="mx-auto h-6 w-6 text-muted-foreground" />
                <p className="mt-3 text-sm font-semibold text-foreground">{t("answerReviewNoMatches")}</p>
                <button
                  type="button"
                  onClick={() => {
                    setFilterParam("questionFilter", "");
                  }}
                  className="mt-2 text-sm font-semibold text-primary hover:underline"
                >
                  {t("answerReviewClearSearch")}
                </button>
              </div>
            </div>
          )}

          <div className="mt-6 flex flex-col-reverse gap-2 pb-8 sm:flex-row sm:items-center sm:justify-end">
            <Link
              to={backHref}
              onClick={(event) => { if (!confirmLeave()) event.preventDefault(); }}
              className="inline-flex h-10 items-center justify-center rounded-[8px] border bg-card px-5 text-sm font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
            >
              {t("answerReviewBackOverview")}
            </Link>
            <Link
              to={`/tasks/${encodeURIComponent(taskId)}/grading-setup`}
              onClick={(event) => { if (!confirmLeave()) event.preventDefault(); }}
              className="inline-flex h-10 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground outline-none hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {t("submissionReviewEnterGradingSetup")}
              <ChevronRight aria-hidden="true" className="h-4 w-4" />
            </Link>
          </div>
        </section>
      )}
    </div>
  );
}

function StudentNavigation({ student, previous, next, onPrevious, onNext, identityOpen, readOnly, onToggleIdentity, t }: {
  student: StudentSubmission;
  previous: StudentSubmission | null;
  next: StudentSubmission | null;
  onPrevious: () => void;
  onNext: () => void;
  identityOpen: boolean;
  readOnly: boolean;
  onToggleIdentity: () => void;
  t: (key: MessageKey) => string;
}) {
  return (
    <nav aria-label={t("answerReviewStudentNavigation")} className="grid min-h-[72px] grid-cols-2 gap-2 rounded-[10px] border bg-card p-2 xl:grid-cols-[minmax(140px,0.75fr)_minmax(360px,1.8fr)_minmax(140px,0.75fr)_145px] xl:items-center">
      <Button type="button" variant="ghost" className="order-2 h-10 justify-start px-3 xl:order-1" disabled={!previous} onClick={onPrevious}>
        <ArrowLeft aria-hidden="true" className="h-4 w-4" />
        <span className="min-w-0 truncate">{previous?.stu_name || t("answerReviewPreviousStudent")}</span>
      </Button>
      <div className="order-1 col-span-2 flex min-h-14 min-w-0 items-center justify-center rounded-[9px] bg-primary/[0.07] px-4 py-2 text-center ring-1 ring-inset ring-primary/10 xl:order-2 xl:col-span-1" title={`${student.stu_id} · ${student.stu_name || student.stu_id}`}>
        <p className="flex min-w-0 flex-wrap items-baseline justify-center gap-x-2 leading-tight text-primary">
          <span className="max-w-full truncate text-[20px] font-extrabold tracking-[-0.02em]">{student.stu_name || student.stu_id}</span>
          <span className="max-w-full truncate text-[15px] font-bold text-primary/75">{student.stu_id}</span>
        </p>
      </div>
      <Button type="button" variant="ghost" className="order-3 h-10 justify-end px-3" disabled={!next} onClick={onNext}>
        <span className="min-w-0 truncate">{next?.stu_name || t("answerReviewNextStudent")}</span>
        <ArrowRight aria-hidden="true" className="h-4 w-4" />
      </Button>
      <Button type="button" variant="secondary" className="order-4 h-10 px-3" disabled={readOnly} onClick={onToggleIdentity}>
        {identityOpen ? <X aria-hidden="true" className="h-4 w-4" /> : <Pencil aria-hidden="true" className="h-4 w-4" />}
        {t(identityOpen ? "studentSubmissionCloseIdentity" : "studentSubmissionEditIdentity")}
      </Button>
    </nav>
  );
}

function SmartPicker({ label, placeholder, query, matches, currentId, onCommit, onSelect, t }: {
  label: string;
  placeholder: string;
  query: string;
  matches: PickerMatch[];
  currentId: string;
  onCommit: (value: string) => void;
  onSelect: (id: string) => void;
  t: (key: MessageKey) => string;
}) {
  const [open, setOpen] = useState(false);
  const smartSearch = useImeSafeQuery({ value: query, onCommit, onDraftChange: () => setOpen(true) });

  function handleBlur(event: FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }

  return (
    <div className="relative" onFocusCapture={() => setOpen(true)} onBlurCapture={handleBlur}>
      <label className="relative block">
        <span className="sr-only">{label}</span>
        <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <input
          type="text"
          inputMode="search"
          value={smartSearch.draftValue}
          onBlur={smartSearch.handleBlur}
          onCompositionStart={smartSearch.handleCompositionStart}
          onCompositionEnd={smartSearch.handleCompositionEnd}
          onChange={smartSearch.handleChange}
          placeholder={placeholder}
          className="h-10 w-full rounded-[7px] border-0 bg-slate-50 pl-9 pr-9 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/20 dark:bg-slate-900/50"
        />
        {smartSearch.draftValue ? (
          <button
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              smartSearch.commitValue("");
            }}
            className="absolute right-2 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={t("answerReviewClearSearch")}
          >
            <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
          </button>
        ) : null}
      </label>
      {open ? (
        <div className="absolute left-0 right-0 top-[44px] z-40 max-h-[280px] overflow-auto rounded-[9px] border bg-card p-1.5 shadow-xl">
          <p className="px-2 py-1 text-[10px] leading-4 text-muted-foreground">
            {smartSearch.draftValue ? `${matches.length} ${t("answerReviewMatches")}` : t("answerReviewLocalMatchHint")}
          </p>
          {matches.length ? matches.slice(0, 40).map(({ item, kind }) => (
            <button
              key={item.id}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onSelect(item.id);
                setOpen(false);
              }}
              className={cn(
                "flex min-h-[44px] w-full items-center justify-between gap-3 rounded-[7px] px-2.5 py-1.5 text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring",
                item.attention && "bg-amber-50/80 hover:bg-amber-100/75 dark:bg-amber-950/25 dark:hover:bg-amber-950/40",
                item.id === currentId && !item.attention && "bg-primary/[0.055]",
                item.id === currentId && item.attention && "ring-1 ring-inset ring-amber-300/70 dark:ring-amber-800",
              )}
            >
              <span className="min-w-0">
                <span className="block truncate text-xs font-semibold text-foreground">{item.primary}</span>
                <span className="block truncate text-[10px] text-muted-foreground">{item.secondary}</span>
              </span>
              {item.attention ? (
                <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/70 dark:text-amber-200">
                  {t("historyNeedsAttention")}
                </span>
              ) : kind !== "all" ? (
                <span className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold",
                  kind === "exact" ? "bg-teal-100 text-teal-700 dark:bg-teal-950/60 dark:text-teal-200" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
                )}>
                  {t(kind === "exact" ? "answerReviewExactMatch" : "answerReviewRelatedMatch")}
                </span>
              ) : null}
            </button>
          )) : (
            <p className="px-3 py-5 text-center text-xs text-muted-foreground">{t("answerReviewNoMatches")}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function AnswerReviewCard({ question, answer, draft, sourceFilename, previous, next, dirty, saving, saveError, editing, readOnly, onDraftChange, onSave, onEdit, onCancel, onConfirm, onNavigate, locale, t }: {
  question: SubmissionQuestion;
  answer?: StudentAnswerInfo;
  draft: AnswerDraft;
  sourceFilename?: string | null;
  previous: SubmissionQuestion | null;
  next: SubmissionQuestion | null;
  dirty: boolean;
  saving: boolean;
  saveError?: string;
  editing: boolean;
  readOnly: boolean;
  onDraftChange: (patch: Partial<AnswerDraft>) => void;
  onSave: (moveNext: boolean) => void;
  onEdit: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  onNavigate: (id: string) => void;
  locale: Locale;
  t: (key: MessageKey) => string;
}) {
  const state = getAnswerState(answer);
  return (
    <article id={questionAnchorId(question.id)} data-question-id={question.id} className="scroll-mt-[86px] overflow-hidden rounded-[10px] border bg-card">
      <header className="flex flex-col gap-3 border-b px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[20px] font-bold text-foreground">{tx(locale, `第 ${question.label} 题`, `Question ${question.label}`)}</h2>
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-muted-foreground dark:bg-slate-800">{question.type || t("studentSubmissionUnknownType")}</span>
            <AnswerStateBadge state={state} answer={answer} locale={locale} t={t} />
            {!readOnly && !editing && !["recognized", "reviewed"].includes(state) ? (
              <Button
                type="button"
                variant="secondary"
                className="h-7 border-amber-200 bg-amber-50 px-3 text-[11px] font-semibold text-amber-700 hover:bg-amber-100 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
                disabled={saving}
                onClick={onConfirm}
              >
                {saving ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Check aria-hidden="true" className="h-3.5 w-3.5" />}
                {t(saving ? "answerReviewConfirming" : "answerReviewConfirm")}
              </Button>
            ) : null}
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">{tx(locale, "题目与学生作答在同一卡片内连续校对；题目只读。", "Review the read-only question and student response together in this card.")}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button type="button" variant="secondary" className="h-9 px-3" disabled={!previous} onClick={() => previous && onNavigate(previous.id)}>
            <ArrowUp aria-hidden="true" className="h-4 w-4" />{t("answerReviewPreviousQuestion")}
          </Button>
          <Button type="button" variant="secondary" className="h-9 px-3" disabled={!next} onClick={() => next && onNavigate(next.id)}>
            {t("answerReviewNextQuestion")}<ArrowDown aria-hidden="true" className="h-4 w-4" />
          </Button>
        </div>
      </header>

      <div className="p-5">
        <section>
          <h3 className="text-sm font-semibold text-foreground">{t("answerReviewQuestionContextTitle")}</h3>
          <div className="mt-3 min-h-[72px] rounded-[8px] bg-slate-50 px-4 py-3 dark:bg-slate-900/45">
            {question.stem ? <MarkdownMath className="text-foreground">{question.stem}</MarkdownMath> : <p className="text-sm text-muted-foreground">{t("answerReviewStemUnavailable")}</p>}
          </div>
        </section>

        <section className="mt-5">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-foreground">{t("answerReviewRecognizedTextTitle")}</h3>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {tx(locale, editing ? "正在编辑原始 LaTeX；保存后恢复渲染。" : "浏览状态渲染 LaTeX；需要修正时再编辑原始文本。", editing ? "Editing raw LaTeX; rendering returns after save." : "LaTeX is rendered while browsing; edit the source only when needed.")}
              </p>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {sourceFilename ? <span className="inline-flex max-w-[300px] items-center gap-1.5 truncate text-[11px] text-muted-foreground" title={sourceFilename}><FileText aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />{sourceFilename}</span> : null}
              {!editing && !readOnly ? (
                <Button type="button" variant="secondary" className="h-8 px-3" onClick={onEdit}>
                  <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
                  {tx(locale, "修改", "Edit")}
                </Button>
              ) : null}
            </div>
          </div>
          {editing ? (
            <>
              <textarea
                value={draft.content}
                onChange={(event) => onDraftChange({ content: event.target.value })}
                placeholder={t("answerReviewContentPlaceholder")}
                className="mt-3 min-h-[132px] w-full resize-y rounded-[8px] border bg-slate-50 px-4 py-3 font-mono text-sm leading-6 text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:bg-card focus:ring-2 focus:ring-primary/15 dark:bg-slate-900/45"
              />
              <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:justify-end">
                <Button type="button" variant="secondary" className="h-9 px-4" disabled={saving} onClick={onCancel}>
                  <X aria-hidden="true" className="h-4 w-4" />
                  {tx(locale, "取消", "Cancel")}
                </Button>
                <Button type="button" variant="secondary" className="h-9 px-4" disabled={saving || !dirty} onClick={() => onSave(false)}>
                  {saving ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <Save aria-hidden="true" className="h-4 w-4" />}
                  {t(saving ? "answerReviewSaving" : "answerReviewSave")}
                </Button>
                <Button type="button" className="h-9 px-4" disabled={saving || !dirty} onClick={() => onSave(Boolean(next))}>
                  <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
                  {next ? t("answerReviewSaveNext") : t("answerReviewSave")}
                  {next ? <ArrowDown aria-hidden="true" className="h-4 w-4" /> : null}
                </Button>
              </div>
            </>
          ) : (
            <div className="mt-3 min-h-[96px] rounded-[8px] bg-slate-50 px-4 py-3 text-sm leading-6 dark:bg-slate-900/45">
              {answer?.content?.trim() ? (
                <MarkdownMath className="text-foreground">{answer.content}</MarkdownMath>
              ) : (
                <p className="text-muted-foreground">{t(state === "missing" ? "studentSubmissionMissingAnswer" : "studentSubmissionBlankAnswer")}</p>
              )}
            </div>
          )}
        </section>

        {saveError ? <p className="mt-3 rounded-[8px] border border-red-200 bg-red-50 px-4 py-2.5 text-xs font-medium text-red-700" role="alert">{saveError}</p> : null}
      </div>
    </article>
  );
}

function AnswerStateBadge({ state, answer, locale, t }: {
  state: SubmissionAnswerState;
  answer?: StudentAnswerInfo;
  locale: Locale;
  t: (key: MessageKey) => string;
}) {
  const flagText = (answer?.flag ?? []).join(" · ");
  const lowConfidence = /低置信|low[ -]?confidence|confidence/i.test(flagText);
  return (
    <span className={cn(
      "inline-flex h-7 shrink-0 items-center justify-center rounded-full px-3 text-[11px] font-semibold",
      state === "recognized" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/70 dark:text-emerald-200",
      state === "reviewed" && "bg-blue-100 text-primary dark:bg-blue-950/60 dark:text-blue-200",
      state === "flagged" && "bg-amber-100 text-amber-700 dark:bg-amber-950/70 dark:text-amber-200",
      (state === "empty" || state === "missing") && "bg-red-100 text-red-600 dark:bg-red-950/60 dark:text-red-200",
    )} title={flagText || undefined}>
      {state === "flagged" && lowConfidence ? tx(locale, "低置信", "Low confidence") : t(STATUS_KEYS[state])}
    </span>
  );
}

function ReviewMetric({ label, value, detail, tone = "primary" }: {
  label: string;
  value: string;
  detail: string;
  tone?: "primary" | "accent" | "warning" | "danger" | "neutral";
}) {
  return (
    <div className="min-h-[90px] rounded-[10px] border bg-card px-4 py-3.5 sm:px-5">
      <dt className="text-[12px] font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-2 flex min-w-0 items-baseline gap-2">
        <span className={cn(
          "min-w-0 truncate text-[22px] font-bold leading-7 tracking-[-0.02em]",
          tone === "primary" && "text-primary",
          tone === "accent" && "text-teal-600 dark:text-teal-300",
          tone === "warning" && "text-amber-600 dark:text-amber-300",
          tone === "danger" && "text-red-500 dark:text-red-300",
          tone === "neutral" && "text-foreground",
        )} title={value}>
          {value}
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{detail}</span>
      </dd>
    </div>
  );
}

function PageState({ title, description, action, onAction, href, busy = false }: {
  title: string;
  description?: string;
  action?: string;
  onAction?: () => void;
  href?: string;
  busy?: boolean;
}) {
  const actionClass = "mt-4 inline-flex h-9 items-center justify-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div className="mt-[45px] flex min-h-[430px] items-center justify-center rounded-[10px] border bg-card px-6 py-10 text-center" role={busy ? "status" : undefined}>
      <div className="max-w-md">
        {busy ? <LoaderCircle aria-hidden="true" className="mx-auto h-7 w-7 animate-spin text-primary" /> : <AlertCircle aria-hidden="true" className="mx-auto h-7 w-7 text-muted-foreground" />}
        <p className="mt-4 text-sm font-semibold text-foreground">{title}</p>
        {description ? <p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p> : null}
        {action && onAction ? <button type="button" className={actionClass} onClick={onAction}>{action}</button> : null}
        {action && href ? <Link className={actionClass} to={href}>{action}</Link> : null}
      </div>
    </div>
  );
}

function matchPickerItems(items: PickerItem[], query: string): PickerMatch[] {
  const normalized = normalize(query);
  if (!normalized) return items.map((item) => ({ item, kind: "all" }));
  return items
    .map((item): PickerMatch | null => {
      if (item.exactValues.some((value) => normalizeComparable(value) === normalizeComparable(normalized))) return { item, kind: "exact" };
      const descriptor = normalize(item.searchable);
      const terms = normalized.split(/\s+/).filter(Boolean);
      if (descriptor.includes(normalized) || terms.every((term) => descriptor.includes(term))) return { item, kind: "related" };
      return null;
    })
    .filter((match): match is PickerMatch => match !== null)
    .sort((a, b) => Number(b.kind === "exact") - Number(a.kind === "exact"));
}

function questionPickerItem(question: SubmissionQuestion): PickerItem {
  const descriptor = `${question.label} ${question.id} ${question.type} ${question.stem}`;
  return {
    id: question.id,
    primary: `Q${question.label}`,
    secondary: question.type || question.stem || question.id,
    searchable: `${descriptor} ${questionSearchAliases(descriptor)}`,
    exactValues: [question.id, question.label, `q${question.label}`, `第${question.label}题`, question.type],
  };
}

function selectMatched<T>(values: T[], matches: PickerMatch[], query: string, getId: (value: T) => string, keepAllWhenEmpty = false) {
  if (!query.trim()) return values;
  if (!matches.length) return keepAllWhenEmpty ? values : [];
  const ids = new Set(matches.map((match) => match.item.id));
  return values.filter((value) => ids.has(getId(value)));
}

function neighbors<T>(values: T[], currentId: string | undefined, getId: (value: T) => string) {
  const index = values.findIndex((value) => getId(value) === currentId);
  return {
    previous: index > 0 ? values[index - 1] : null,
    next: index >= 0 && index < values.length - 1 ? values[index + 1] : null,
  };
}

function buildBackHref(taskId: string | undefined, searchParams: URLSearchParams) {
  if (!taskId) return "/history";
  const returnParams = searchParams.get("returnParams");
  return `/tasks/${encodeURIComponent(taskId)}/submissions${returnParams ? `?${returnParams}` : ""}`;
}

function answerErrorMessage(error: unknown, t: (key: MessageKey) => string) {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(normalized) ?? "";
  if (code === "task_workflow_changed") return t("answerReviewStale");
  if (code === "task_workflow_busy" || normalized.status === 409) return t("answerReviewUnavailable");
  return t("answerReviewSaveError");
}

function isKeyboardNavigationBlocked(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable='true'], [role='dialog']"));
}

function scrollQuestionIntoView(questionId: string, behavior: ScrollBehavior) {
  const element = document.getElementById(questionAnchorId(questionId));
  if (!element) return;
  const top = window.scrollY + element.getBoundingClientRect().top - 86;
  window.scrollTo({ top: Math.max(0, top), left: 0, behavior });
}

function questionAnchorId(questionId: string) {
  return `answer-question-${questionId}`;
}

function normalize(value: string) {
  return value.trim().toLocaleLowerCase();
}

function normalizeComparable(value: string) {
  return normalize(value).replace(/第|题|\s/g, "").replace(/^q/i, "");
}

function studentPath(taskId: string, studentId: string) {
  return `/tasks/${encodeURIComponent(taskId)}/students/${encodeURIComponent(studentId)}`;
}

function formatPercent(numerator: number, denominator: number) {
  if (!denominator) return "—";
  return `${Math.round((numerator / denominator) * 100)}%`;
}

function identityMethodKey(method: NonNullable<StudentSubmission["identity_match_method"]>): MessageKey {
  if (method === "roster") return "studentSubmissionIdentityMethodRoster";
  if (method === "manual_review") return "studentSubmissionIdentityMethodManual";
  return "studentSubmissionIdentityMethodFilename";
}

function identityErrorMessage(error: unknown, t: (key: MessageKey) => string) {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(normalized) ?? "";
  if (code === "student_identity_conflict") return t("studentSubmissionIdentityConflict");
  if (isWorkflowRevisionConflictCode(code)) return t("studentSubmissionIdentityStale");
  if (code === "student_identity_edit_unavailable") return t("studentSubmissionIdentityUnavailable");
  if (code === "student_identity_required") return t("studentSubmissionIdentityRequired");
  return t("studentSubmissionIdentityError");
}

function compareStudents(a: StudentSubmission, b: StudentSubmission) {
  return a.stu_id.localeCompare(b.stu_id, undefined, { numeric: true, sensitivity: "base" });
}

function tx(locale: Locale, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

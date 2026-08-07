import {
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Check,
  Keyboard,
  LoaderCircle,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, Navigate, useBlocker, useNavigate, useParams, useSearchParams, type BlockerFunction } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useTask, useTaskResult, useUpdateCorrectionReview } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import {
  buildResultsModel,
  correctionScoreSource,
  correctionReviewDraftScore,
  displayableCorrectionScore,
  effectiveCorrectionScore,
  formatConfidence,
  formatScore,
  reviewConfirmationScore,
  shouldHideAutomatedScores,
  type QuestionSummary,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import { collectResultReviewItems } from "@/components/tasks/resultsReviewModel";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import {
  matchReviewItems,
  questionSearchItems,
  type ReviewSearchMatch,
} from "@/lib/reviewDetail";
import { reviewCellKey } from "@/lib/reviewOverview";
import { getSafeTaskReturnTo, getTaskDestination } from "@/lib/taskFlow";
import { ResultQuestionSidebar, type ResultQuestionState } from "@/routes/tasks/results/ResultQuestionSidebar";
import type { Correction } from "@/types";

type ReviewDraft = {
  score: string;
  comment: string;
};

/** R02: one student, every question, one continuous and auditable teacher-review workspace. */
export function ReviewDetailPage() {
  const { taskId, studentId, questionId } = useParams();
  const { locale, t } = useI18n();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const taskQuery = useTask(taskId);
  const resultQuery = useTaskResult(taskId);
  const updateReview = useUpdateCorrectionReview();
  const model = useMemo(() => buildResultsModel(taskQuery.data, resultQuery.data), [resultQuery.data, taskQuery.data]);
  const student = model.students.find((item) => item.id === studentId) ?? null;
  const requestedQuestionId = questionId === "all" ? "" : questionId ?? "";
  const questionQuery = searchParams.get("question") ?? "";
  const overviewHref = taskId
    ? getSafeTaskReturnTo(taskId, searchParams.get("returnTo")) ?? `/tasks/${encodeURIComponent(taskId)}/review`
    : "/history";

  const questionMatches = useMemo(
    () => matchReviewItems(questionSearchItems(model.questions), questionQuery),
    [model.questions, questionQuery],
  );
  const questionById = useMemo(
    () => new Map(model.questions.map((item) => [item.id, item])),
    [model.questions],
  );
  const visibleQuestions = useMemo(
    () => questionMatches
      .map((match) => questionById.get(match.item.id))
      .filter((item): item is QuestionSummary => Boolean(item)),
    [questionById, questionMatches],
  );
  const correctionByQuestionId = useMemo(
    () => new Map(student?.corrections.map((item) => [item.q_id, item]) ?? []),
    [student?.corrections],
  );
  const reviewItems = useMemo(() => collectResultReviewItems(model, model.students), [model]);
  const pendingReviewItems = useMemo(
    () => reviewItems.filter((item) => {
      const source = correctionScoreSource(item.correction);
      return source === "ai_untouched" || source === "hard_failure";
    }),
    [reviewItems],
  );
  const blockingReviewItems = useMemo(
    () => reviewItems.filter((item) => effectiveCorrectionScore(item.correction) === null),
    [reviewItems],
  );
  const requiredReviewKeys = useMemo(
    () => new Set(reviewItems.map((item) => reviewCellKey(item.student.id, item.question.id))),
    [reviewItems],
  );
  const reviewReasonsByKey = useMemo(
    () => new Map(reviewItems.map((item) => [reviewCellKey(item.student.id, item.question.id), item.reasons])),
    [reviewItems],
  );
  const studentIndex = model.students.findIndex((item) => item.id === studentId);
  const previousStudent = studentIndex > 0 ? model.students[studentIndex - 1] : null;
  const nextStudent = studentIndex >= 0 && studentIndex < model.students.length - 1
    ? model.students[studentIndex + 1]
    : null;
  const [activeQuestionId, setActiveQuestionId] = useState(requestedQuestionId);
  const activeIndex = Math.max(0, visibleQuestions.findIndex((question) => question.id === activeQuestionId));
  const activeQuestion = visibleQuestions[activeIndex] ?? visibleQuestions[0] ?? null;
  const previousQuestion = activeIndex > 0 ? visibleQuestions[activeIndex - 1] : null;
  const nextQuestion = activeIndex >= 0 && activeIndex < visibleQuestions.length - 1
    ? visibleQuestions[activeIndex + 1]
    : null;

  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  const [scoreErrors, setScoreErrors] = useState<Record<string, string>>({});
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const [savingQuestionId, setSavingQuestionId] = useState<string | null>(null);
  const [pendingQuestionNavigationId, setPendingQuestionNavigationId] = useState<string | null>(null);
  const [dialogSaving, setDialogSaving] = useState(false);
  const [dialogSaveError, setDialogSaveError] = useState<string | undefined>();
  const initializedStudentRef = useRef<string | null>(null);
  const positionedRouteRef = useRef<string | null>(null);
  const resetScrollForStudentRef = useRef<string | null>(null);
  const searchParamsRef = useRef(searchParams);
  const bypassNavigationRef = useRef(false);

  useEffect(() => {
    searchParamsRef.current = searchParams;
  }, [searchParams]);

  useEffect(() => {
    if (!student || initializedStudentRef.current === student.id) return;
    initializedStudentRef.current = student.id;
    setDrafts(Object.fromEntries(student.corrections.map((correction) => [
      correction.q_id,
      {
        score: correctionReviewDraftScore(correction),
        comment: correction.teacher_comment ?? "",
      },
    ])));
    setScoreErrors({});
    setSaveErrors({});
    setSavingQuestionId(null);
  }, [student]);

  const dirtyQuestionIds = useMemo(() => new Set(student?.corrections.flatMap((correction) => {
    const draft = drafts[correction.q_id];
    if (!draft) return [];
    const changed = draft.score !== correctionReviewDraftScore(correction)
      || draft.comment !== (correction.teacher_comment ?? "");
    return changed ? [correction.q_id] : [];
  }) ?? []), [drafts, student]);
  const dirty = dirtyQuestionIds.size > 0;
  const shouldBlockNavigation = useCallback<BlockerFunction>(({ currentLocation, nextLocation }) => (
    dirty
    && !bypassNavigationRef.current
    && currentLocation.pathname !== nextLocation.pathname
  ), [dirty]);
  const blocker = useBlocker(shouldBlockNavigation);

  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const buildHref = useCallback((nextStudentId: string, nextQuestionId: string, preserveFilters = true) => {
    const nextParams = preserveFilters ? new URLSearchParams(searchParams) : new URLSearchParams();
    nextParams.delete("student");
    if (!preserveFilters) {
      const returnTo = searchParams.get("returnTo");
      if (returnTo) nextParams.set("returnTo", returnTo);
    }
    const serialized = nextParams.toString();
    return `/tasks/${encodeURIComponent(taskId ?? "")}/review/${encodeURIComponent(nextStudentId)}/${encodeURIComponent(nextQuestionId)}${serialized ? `?${serialized}` : ""}`;
  }, [searchParams, taskId]);

  const navigateWithoutBlocking = useCallback((href: string) => {
    bypassNavigationRef.current = true;
    navigate(href);
    window.setTimeout(() => {
      bypassNavigationRef.current = false;
    }, 0);
  }, [navigate]);

  const setFilter = useCallback((key: "question", value: string) => {
    const next = new URLSearchParams(searchParamsRef.current);
    next.delete("student");
    if (value.trim()) next.set(key, value);
    else next.delete(key);
    searchParamsRef.current = next;
    setSearchParams(next, { replace: true });
  }, [setSearchParams]);

  const goToStudent = useCallback((nextStudentId: string) => {
    const anchorQuestionId = activeQuestion?.id ?? visibleQuestions[0]?.id ?? model.questions[0]?.id;
    if (!anchorQuestionId) return;
    resetScrollForStudentRef.current = nextStudentId;
    navigate(buildHref(nextStudentId, anchorQuestionId));
  }, [activeQuestion?.id, buildHref, model.questions, navigate, visibleQuestions]);

  const scrollToQuestion = useCallback((targetId: string, behavior: ScrollBehavior = "smooth") => {
    setActiveQuestionId(targetId);
    const element = document.getElementById(questionAnchorId(targetId));
    if (!element) return;
    const top = window.scrollY + element.getBoundingClientRect().top - 86;
    window.scrollTo({ top: Math.max(0, top), left: 0, behavior });
  }, []);

  const requestQuestionNavigation = useCallback((targetId: string) => {
    if (targetId === activeQuestionId) {
      scrollToQuestion(targetId);
      return;
    }
    if (dirty) {
      setDialogSaveError(undefined);
      setPendingQuestionNavigationId(targetId);
      return;
    }
    scrollToQuestion(targetId);
  }, [activeQuestionId, dirty, scrollToQuestion]);

  useEffect(() => {
    if (!visibleQuestions.length || !studentId) return;
    const requested = visibleQuestions.find((question) => question.id === requestedQuestionId) ?? visibleQuestions[0];
    const routeKey = `${studentId}:${requestedQuestionId}:${questionQuery}`;
    if (positionedRouteRef.current === routeKey) return;
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        positionedRouteRef.current = routeKey;
        setActiveQuestionId(requested.id);
        if (resetScrollForStudentRef.current === studentId) {
          resetScrollForStudentRef.current = null;
          window.scrollTo({ top: 0, left: 0, behavior: "auto" });
        } else {
          scrollToQuestion(requested.id, "auto");
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [questionQuery, requestedQuestionId, scrollToQuestion, studentId, visibleQuestions]);

  useEffect(() => {
    if (!visibleQuestions.length) return;
    let frame = 0;
    const updateActiveQuestion = () => {
      frame = 0;
      const root = document.scrollingElement ?? document.documentElement;
      const atEnd = root.scrollHeight > window.innerHeight + 1
        && window.scrollY + window.innerHeight >= root.scrollHeight - 2;
      let nextActive = atEnd ? visibleQuestions[visibleQuestions.length - 1]?.id ?? "" : visibleQuestions[0]?.id ?? "";
      if (!atEnd) {
        for (const question of visibleQuestions) {
          const element = document.getElementById(questionAnchorId(question.id));
          if (!element || element.getBoundingClientRect().top > 112) break;
          nextActive = question.id;
        }
      }
      if (nextActive) setActiveQuestionId(nextActive);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(updateActiveQuestion);
    };
    updateActiveQuestion();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [visibleQuestions]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (isInteractiveTarget(event.target)) return;
      if (event.key === "ArrowLeft" && previousStudent) {
        event.preventDefault();
        goToStudent(previousStudent.id);
      } else if (event.key === "ArrowRight" && nextStudent) {
        event.preventDefault();
        goToStudent(nextStudent.id);
      } else if (event.key === "ArrowUp" && previousQuestion) {
        event.preventDefault();
        requestQuestionNavigation(previousQuestion.id);
      } else if (event.key === "ArrowDown" && nextQuestion) {
        event.preventDefault();
        requestQuestionNavigation(nextQuestion.id);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [goToStudent, nextQuestion, nextStudent, previousQuestion, previousStudent, requestQuestionNavigation]);

  if (taskId && taskQuery.data?.status === "grading") return <Navigate replace to={`/tasks/${taskId}/grading/progress`} />;
  if (taskId && taskQuery.data && ![
    "graded", "review_confirmed", "generating_analysis", "finalized",
  ].includes(taskQuery.data.status)) return <Navigate replace to={getTaskDestination(taskQuery.data)} />;

  const loading = taskQuery.isLoading || resultQuery.isLoading;
  const failed = taskQuery.isError || resultQuery.isError;

  function updateDraft(qId: string, patch: Partial<ReviewDraft>) {
    setDrafts((current) => ({
      ...current,
      [qId]: { ...(current[qId] ?? { score: "", comment: "" }), ...patch },
    }));
    setScoreErrors((current) => omitKey(current, qId));
    setSaveErrors((current) => omitKey(current, qId));
  }

  function prepareConfirmedReview(question: QuestionSummary) {
    if (!taskId || !student) {
      return { ok: false as const, message: tx(locale, "缺少任务或学生信息。", "Task or student information is missing.") };
    }
    const correction = correctionByQuestionId.get(question.id);
    if (!correction) {
      return { ok: false as const, message: tx(locale, "找不到这道题的批改结果。", "This grading result could not be found.") };
    }
    const draft = drafts[question.id] ?? {
      score: correctionReviewDraftScore(correction),
      comment: correction.teacher_comment ?? "",
    };
    const normalizedScore = draft.score.trim();
    const numericScore = reviewConfirmationScore(correction, draft.score);
    if (!normalizedScore && numericScore === null) {
      setScoreErrors((current) => ({
        ...current,
        [question.id]: tx(locale, "SmarTAI 没有生成有效分数，请先手动输入最终分。", "SmarTAI did not produce a valid score. Enter a final score manually."),
      }));
      return { ok: false as const, message: tx(locale, "请先为无分结果填写最终分。", "Enter a final score for the unscored result first.") };
    }
    if (numericScore === null || !Number.isFinite(numericScore) || numericScore < 0 || numericScore > correction.max_score) {
      setScoreErrors((current) => ({
        ...current,
        [question.id]: tx(locale, `请输入 0–${formatScore(correction.max_score)} 之间的分数。`, `Enter a score from 0 to ${formatScore(correction.max_score)}.`),
      }));
      return { ok: false as const, message: tx(locale, "请先修正标红的分数。", "Correct the highlighted score first.") };
    }
    return {
      ok: true as const,
      numericScore,
      teacherComment: draft.comment,
    };
  }

  async function persistConfirmedReview(question: QuestionSummary, expectedWorkflowRevision: number) {
    const prepared = prepareConfirmedReview(question);
    if (!prepared.ok) return prepared;
    if (!taskId || !student) {
      return { ok: false as const, message: tx(locale, "缺少任务或学生信息。", "Task or student information is missing.") };
    }
    setSavingQuestionId(question.id);
    setScoreErrors((current) => omitKey(current, question.id));
    setSaveErrors((current) => omitKey(current, question.id));
    try {
      const response = await updateReview.mutateAsync({
        taskId,
        studentId: student.id,
        qId: question.id,
        expected_workflow_revision: expectedWorkflowRevision,
        teacher_score: prepared.numericScore,
        teacher_comment: prepared.teacherComment,
        confirm: true,
      });
      setDrafts((current) => ({
        ...current,
        [question.id]: {
          score: correctionReviewDraftScore(response.correction),
          comment: response.correction.teacher_comment ?? "",
        },
      }));
      return { ok: true as const, workflowRevision: response.workflow_revision };
    } catch (error) {
      const normalized = normalizeAPIError(error);
      setSaveErrors((current) => ({ ...current, [question.id]: normalized.message }));
      if (normalized.status === 409) void Promise.all([taskQuery.refetch(), resultQuery.refetch()]);
      return { ok: false as const, message: normalized.message };
    } finally {
      setSavingQuestionId(null);
    }
  }

  function continueAfterQuestion(question: QuestionSummary, guardOtherUnsavedChanges: boolean) {
    const currentKey = student ? reviewCellKey(student.id, question.id) : "";
    const nextReview = pendingReviewItems.find((item) => reviewCellKey(item.student.id, item.question.id) !== currentKey);
    if (!nextReview) {
      if (guardOtherUnsavedChanges) navigate(overviewHref);
      else navigateWithoutBlocking(overviewHref);
      return;
    }
    if (nextReview.student.id === student?.id && visibleQuestions.some((item) => item.id === nextReview.question.id)) {
      window.requestAnimationFrame(() => {
        if (guardOtherUnsavedChanges) requestQuestionNavigation(nextReview.question.id);
        else scrollToQuestion(nextReview.question.id);
      });
      return;
    }
    const href = buildHref(nextReview.student.id, nextReview.question.id, false);
    if (guardOtherUnsavedChanges) navigate(href);
    else navigateWithoutBlocking(href);
  }

  async function confirmAndContinue(question: QuestionSummary) {
    if (!taskQuery.data) return;
    const correction = correctionByQuestionId.get(question.id);
    const source = correction ? correctionScoreSource(correction) : null;
    if ((source === "teacher_confirmed_same" || source === "teacher_changed") && !dirtyQuestionIds.has(question.id)) {
      continueAfterQuestion(question, dirtyQuestionIds.size > 0);
      return;
    }
    const wasDirty = dirtyQuestionIds.has(question.id);
    const hasOtherDirtyChanges = Array.from(dirtyQuestionIds).some((qId) => qId !== question.id);
    const outcome = await persistConfirmedReview(question, taskQuery.data.workflow_revision);
    if (!outcome.ok) return;
    toast.success(wasDirty
      ? tx(locale, "修改已保存并确认，正在继续复核", "Changes saved and confirmed. Continuing review.")
      : tx(locale, "该题复核结果已确认", "This question review is confirmed"));
    continueAfterQuestion(question, hasOtherDirtyChanges);
  }

  async function saveAllDirtyReviews() {
    if (!taskQuery.data) return false;
    const questionIds = Array.from(dirtyQuestionIds);
    for (const qId of questionIds) {
      const question = model.questions.find((item) => item.id === qId);
      if (!question) {
        setDialogSaveError(tx(locale, "找不到待保存的题目，请刷新后重试。", "A changed question could not be found. Refresh and try again."));
        return false;
      }
      const prepared = prepareConfirmedReview(question);
      if (!prepared.ok) {
        setDialogSaveError(prepared.message);
        return false;
      }
    }
    let workflowRevision = taskQuery.data.workflow_revision;
    let confirmedCount = 0;
    for (const qId of questionIds) {
      const question = model.questions.find((item) => item.id === qId);
      if (!question) continue;
      const outcome = await persistConfirmedReview(question, workflowRevision);
      if (!outcome.ok) {
        const remainingCount = questionIds.length - confirmedCount;
        setDialogSaveError(confirmedCount > 0
          ? tx(
              locale,
              `已有 ${confirmedCount} 处修改保存并确认；其余 ${remainingCount} 处尚未完成。请检查后重试。${outcome.message}`,
              `${confirmedCount} change${confirmedCount === 1 ? " was" : "s were"} saved and confirmed; ${remainingCount} remain. Check the error and retry. ${outcome.message}`,
            )
          : outcome.message);
        return false;
      }
      workflowRevision = outcome.workflowRevision;
      confirmedCount += 1;
    }
    toast.success(tx(
      locale,
      `${questionIds.length} 处修改已保存并确认`,
      `${questionIds.length} change${questionIds.length === 1 ? "" : "s"} saved and confirmed`,
    ));
    return true;
  }

  function restoreDirtyDrafts() {
    if (!student) return;
    setDrafts((current) => {
      const next = { ...current };
      for (const correction of student.corrections) {
        if (!dirtyQuestionIds.has(correction.q_id)) continue;
        next[correction.q_id] = {
          score: correctionReviewDraftScore(correction),
          comment: correction.teacher_comment ?? "",
        };
      }
      return next;
    });
    setScoreErrors({});
    setSaveErrors({});
  }

  function stayOnReview() {
    setDialogSaveError(undefined);
    setPendingQuestionNavigationId(null);
    if (blocker.state === "blocked") blocker.reset();
  }

  function discardAndContinue() {
    const targetQuestionId = pendingQuestionNavigationId;
    restoreDirtyDrafts();
    setDialogSaveError(undefined);
    setPendingQuestionNavigationId(null);
    if (blocker.state === "blocked") {
      blocker.proceed();
    } else if (targetQuestionId) {
      window.requestAnimationFrame(() => scrollToQuestion(targetQuestionId));
    }
  }

  async function saveAndContinue() {
    setDialogSaving(true);
    setDialogSaveError(undefined);
    const saved = await saveAllDirtyReviews();
    setDialogSaving(false);
    if (!saved) return;
    const targetQuestionId = pendingQuestionNavigationId;
    setPendingQuestionNavigationId(null);
    if (blocker.state === "blocked") {
      blocker.proceed();
    } else if (targetQuestionId) {
      window.requestAnimationFrame(() => scrollToQuestion(targetQuestionId));
    }
  }

  const lockedResultsReason = blockingReviewItems.length
    ? tx(
        locale,
        `结果分析尚未解锁：还有 ${blockingReviewItems.length} 个题次没有有效分数，必须先处理。`,
        `Results & Analysis is locked: ${blockingReviewItems.length} response${blockingReviewItems.length === 1 ? " has" : "s have"} no valid score and must be resolved.`,
      )
    : tx(
        locale,
        "没有阻断项；未人工处理的有效 AI 分会默认沿用。请在复核总览确认正式结果。",
        "No blocking items remain. Valid untouched AI scores will be used by default; confirm the final result in the review overview.",
      );

  function activateLockedResults() {
    toast.info(lockedResultsReason);
    if (dirty) {
      navigate(overviewHref);
      return;
    }
    const first = blockingReviewItems[0];
    if (!first) {
      navigate(overviewHref);
    } else if (first.student.id === student?.id && visibleQuestions.some((item) => item.id === first.question.id)) {
      scrollToQuestion(first.question.id);
    } else {
      navigate(buildHref(first.student.id, first.question.id, false));
    }
  }

  return (
    <div className="w-full max-w-[1300px] pb-8">
      <div className="flex min-h-9 flex-col-reverse items-start justify-between gap-3 sm:flex-row sm:gap-5">
        <div className="min-w-0">
          <h1 className="text-[28px] font-bold leading-9 tracking-[-0.02em] text-foreground sm:text-[30px]">
            {tx(locale, `复核详情：${student?.name ?? "—"}`, `Review Detail: ${student?.name ?? "—"}`)}
          </h1>
          {student ? <p className="mt-1 text-[12px] text-muted-foreground">{student.id} · {tx(locale, "全部题目连续复核", "Continuous review of all questions")}</p> : null}
        </div>
        {taskId ? (
          <Link to={overviewHref} className="inline-flex h-9 shrink-0 self-end items-center gap-1.5 rounded-[8px] border bg-card px-3 text-xs font-semibold text-foreground hover:bg-muted sm:self-auto">
            <ArrowLeft aria-hidden="true" className="h-3.5 w-3.5" />
            {tx(locale, "返回复核总览", "Back to Review Overview")}
          </Link>
        ) : null}
      </div>
      <NewTaskStepper
        currentStep={6}
        lockedStep={7}
        lockedStepReason={lockedResultsReason}
        onLockedStepActivate={activateLockedResults}
      />

      {!taskId ? (
        <PageState title={tx(locale, "缺少任务 ID", "Task ID is missing")} />
      ) : loading ? (
        <PageState title={tx(locale, "正在加载复核详情…", "Loading review detail…")} busy />
      ) : failed ? (
        <PageState title={tx(locale, "无法加载复核详情", "Could not load review detail")} action={tx(locale, "重试", "Retry")} onAction={() => void Promise.all([taskQuery.refetch(), resultQuery.refetch()])} />
      ) : !student || !model.questions.length ? (
        <PageState title={tx(locale, "找不到对应的学生或题目", "Student or question not found")} href={overviewHref} action={tx(locale, "返回总览", "Back to Overview")} />
      ) : (
        <>
          <StudentNavigation
            className="mt-5"
            locale={locale}
            current={{ id: student.id, primary: student.name, secondary: student.id }}
            previous={previousStudent}
            next={nextStudent}
            onSelect={goToStudent}
          />

          <QuestionSearch
            className="mt-3"
            locale={locale}
            value={questionQuery}
            matches={questionMatches}
            onQuery={(value) => setFilter("question", value)}
            onSelect={requestQuestionNavigation}
          />
          <div className="mt-1.5 grid gap-0.5 px-1 text-[11px] leading-5 text-muted-foreground">
            <span>{tx(locale, "搜索只筛选题目，当前学生保持不变；中文输入在选词完成后应用。", "Question search does not change the selected student; IME text is applied after composition.")}</span>
            <span className="flex items-center gap-1.5 font-medium text-foreground/70">
              <Keyboard aria-hidden="true" className="h-3.5 w-3.5" />
              {t("answerReviewKeyboardHint")}
            </span>
          </div>

          {visibleQuestions.length ? (
            <div className="mt-4 grid items-start gap-4 lg:grid-cols-[132px_minmax(0,1fr)]">
              <ResultQuestionSidebar
                locale={locale}
                questions={visibleQuestions}
                activeId={activeQuestion?.id ?? null}
                onSelect={requestQuestionNavigation}
                stateForQuestion={(question) => reviewQuestionState(
                  correctionByQuestionId.get(question.id),
                  requiredReviewKeys.has(reviewCellKey(student.id, question.id)),
                )}
              />
              <main className="min-w-0 space-y-5">
                {visibleQuestions.map((question, index) => {
                  const correction = correctionByQuestionId.get(question.id);
                  const draft = drafts[question.id] ?? {
                    score: correction ? correctionReviewDraftScore(correction) : "",
                    comment: correction?.teacher_comment ?? "",
                  };
                  const required = requiredReviewKeys.has(reviewCellKey(student.id, question.id));
                  return (
                    <ReviewQuestionCard
                      key={question.id}
                      locale={locale}
                      student={student}
                      question={question}
                      correction={correction}
                      draft={draft}
                      required={required}
                      reviewReasons={reviewReasonsByKey.get(reviewCellKey(student.id, question.id)) ?? []}
                      previous={visibleQuestions[index - 1] ?? null}
                      next={visibleQuestions[index + 1] ?? null}
                      dirty={dirtyQuestionIds.has(question.id)}
                      saving={savingQuestionId === question.id || updateReview.isPending}
                      scoreError={scoreErrors[question.id]}
                      saveError={saveErrors[question.id]}
                      onDraftChange={(patch) => updateDraft(question.id, patch)}
                      hasNextReview={pendingReviewItems.some((item) => reviewCellKey(item.student.id, item.question.id) !== reviewCellKey(student.id, question.id))}
                      onConfirm={() => void confirmAndContinue(question)}
                      onNavigate={requestQuestionNavigation}
                    />
                  );
                })}
              </main>
            </div>
          ) : (
            <PageState title={tx(locale, "当前筛选没有题目", "No questions match this filter")} action={tx(locale, "清空题目筛选", "Clear question filter")} onAction={() => setFilter("question", "")} />
          )}

          <div className="mt-6 flex justify-end">
            <Link to={overviewHref} className="inline-flex h-10 items-center justify-center rounded-[8px] border bg-card px-5 text-sm font-semibold text-foreground hover:bg-muted">
              {tx(locale, "返回复核总览", "Back to Review Overview")}
            </Link>
          </div>
        </>
      )}
      {blocker.state === "blocked" || pendingQuestionNavigationId ? (
        <UnsavedChangesDialog
          title={tx(locale, "有批改修改尚未保存", "Unsaved grading changes")}
          description={tx(
            locale,
            `当前有 ${dirtyQuestionIds.size} 处修改尚未保存。你可以继续编辑、放弃修改，或直接保存并确认后继续原操作。`,
            `${dirtyQuestionIds.size} change${dirtyQuestionIds.size === 1 ? " is" : "s are"} unsaved. Keep editing, discard them, or save and confirm them before continuing.`,
          )}
          stayLabel={tx(locale, "继续编辑", "Keep editing")}
          leaveLabel={tx(locale, "放弃修改", "Discard changes")}
          saveLabel={tx(locale, "保存、确认并继续", "Save, confirm & continue")}
          savingLabel={tx(locale, "正在保存…", "Saving…")}
          saving={dialogSaving || updateReview.isPending}
          saveError={dialogSaveError}
          onStay={stayOnReview}
          onLeave={discardAndContinue}
          onSave={() => void saveAndContinue()}
        />
      ) : null}
    </div>
  );
}

function StudentNavigation({ className, locale, current, previous, next, onSelect }: {
  className?: string;
  locale: Locale;
  current: { id: string; primary: string; secondary: string };
  previous: StudentSummary | null;
  next: StudentSummary | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section className={cn("relative grid min-h-[72px] grid-cols-2 gap-2 rounded-[10px] border bg-card p-2 xl:grid-cols-[minmax(150px,0.8fr)_minmax(360px,1.8fr)_minmax(150px,0.8fr)] xl:items-center", className)} aria-label={tx(locale, "学生导航", "Student navigation")}>
      <NavButton className="order-2 xl:order-1" disabled={!previous} onClick={() => previous && onSelect(previous.id)} icon={ArrowLeft} label={previous?.name || tx(locale, "上一位学生", "Previous student")} />
      <div className="order-1 col-span-2 flex min-h-14 min-w-0 items-center justify-center rounded-[9px] bg-primary/[0.07] px-4 py-2 text-center ring-1 ring-inset ring-primary/10 xl:order-2 xl:col-span-1" title={`${current.secondary} · ${current.primary}`}>
        <p className="flex min-w-0 flex-wrap items-baseline justify-center gap-x-2 leading-tight text-primary">
          <span className="max-w-full truncate text-[20px] font-extrabold tracking-[-0.02em]">{current.primary}</span>
          <span className="max-w-full truncate text-[15px] font-bold text-primary/75">{current.secondary}</span>
        </p>
      </div>
      <NavButton className="order-3 xl:order-3" disabled={!next} onClick={() => next && onSelect(next.id)} icon={ArrowRight} label={next?.name || tx(locale, "下一位学生", "Next student")} iconAfter />
    </section>
  );
}

function QuestionSearch({ className, locale, value, matches, onQuery, onSelect }: {
  className?: string;
  locale: Locale;
  value: string;
  matches: ReviewSearchMatch[];
  onQuery: (value: string) => void;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const smartSearch = useImeSafeQuery({ value, onCommit: onQuery, onDraftChange: () => setOpen(true) });

  return (
    <section className={cn("relative rounded-[10px] border bg-card p-2", className)} aria-label={tx(locale, "题目筛选", "Question filter")}>
      <label className="relative block">
        <span className="sr-only">{tx(locale, "SmarTAI 智能搜索题目", "SmarTAI Smart Search for questions")}</span>
        <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <input
          value={smartSearch.draftValue}
          inputMode="search"
          onFocus={() => setOpen(true)}
          onCompositionStart={smartSearch.handleCompositionStart}
          onCompositionEnd={smartSearch.handleCompositionEnd}
          onChange={smartSearch.handleChange}
          onBlur={(event) => {
            smartSearch.handleBlur(event);
            window.setTimeout(() => setOpen(false), 120);
          }}
          placeholder={tx(locale, "SmarTAI 智能搜索：题号、题型、题干或“低置信”", "SmarTAI Smart Search: number, type, stem, or low confidence")}
          className="h-10 w-full rounded-[7px] border-0 bg-slate-50 pl-9 pr-9 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/20 dark:bg-slate-900/50"
        />
        {smartSearch.draftValue ? <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { smartSearch.commitValue(""); setOpen(false); }} className="absolute right-2 top-1/2 inline-flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground" aria-label={tx(locale, "清空题目筛选", "Clear question filter")}><X aria-hidden="true" className="h-3.5 w-3.5" /></button> : null}
      </label>
      {open && smartSearch.draftValue.trim() && smartSearch.draftValue === value ? (
        <div className="absolute left-2 right-2 top-[52px] z-40 max-h-[280px] overflow-auto rounded-[9px] border bg-card p-1.5 shadow-xl">
          {matches.length ? matches.slice(0, 20).map((match) => (
            <button key={match.item.id} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { onSelect(match.item.id); setOpen(false); }} className="flex min-h-[42px] w-full items-center gap-3 rounded-[7px] px-2.5 py-1.5 text-left hover:bg-muted">
              <span className="min-w-12 text-xs font-bold text-foreground">{match.item.primary}</span>
              <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold", match.kind === "exact" ? "bg-teal-100 text-teal-700" : "bg-blue-50 text-primary")}>{match.kind === "exact" ? tx(locale, "完全匹配", "Exact") : tx(locale, "相关匹配", "Related")}</span>
              <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{match.item.secondary || "—"}</span>
            </button>
          )) : <p className="px-3 py-5 text-center text-xs text-muted-foreground">{tx(locale, "没有匹配题目；清空后可恢复全部。", "No questions matched; clear the filter to restore all.")}</p>}
        </div>
      ) : null}
    </section>
  );
}

function ReviewQuestionCard({ locale, student, question, correction, draft, required, reviewReasons, previous, next, dirty, saving, scoreError, saveError, hasNextReview, onDraftChange, onConfirm, onNavigate }: {
  locale: Locale;
  student: StudentSummary;
  question: QuestionSummary;
  correction?: Correction;
  draft: ReviewDraft;
  required: boolean;
  reviewReasons: string[];
  previous: QuestionSummary | null;
  next: QuestionSummary | null;
  dirty: boolean;
  saving: boolean;
  scoreError?: string;
  saveError?: string;
  hasNextReview: boolean;
  onDraftChange: (patch: Partial<ReviewDraft>) => void;
  onConfirm: () => void;
  onNavigate: (id: string) => void;
}) {
  const displayScore = correction ? displayableCorrectionScore(correction) : null;
  const hideAutomatedScores = correction ? shouldHideAutomatedScores(correction) : false;
  const scoreSource = correction ? correctionScoreSource(correction) : null;
  const answer = student.answerByQuestion.get(question.id);
  const alreadyConfirmed = (scoreSource === "teacher_confirmed_same" || scoreSource === "teacher_changed") && !dirty;
  const actionLabel = alreadyConfirmed
    ? hasNextReview
      ? tx(locale, "继续复核", "Continue review")
      : tx(locale, "返回复核总览", "Back to review overview")
    : dirty
      ? hasNextReview
        ? tx(locale, "保存、确认并继续", "Save, confirm & continue")
        : tx(locale, "保存并确认", "Save & confirm")
      : hasNextReview
        ? tx(locale, "确认并继续", "Confirm & continue")
        : tx(locale, "确认复核", "Confirm review");
  return (
    <article id={questionAnchorId(question.id)} data-question-id={question.id} className="scroll-mt-[86px] overflow-hidden rounded-[10px] border bg-card">
      <header className="flex flex-col gap-3 border-b px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[20px] font-bold text-foreground">{question.label}</h2>
            <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-muted-foreground dark:bg-slate-800">{question.type || tx(locale, "未分类", "Uncategorized")}</span>
            <ReviewStatus correction={correction} required={required} locale={locale} />
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">{tx(locale, "题目、作答、SmarTAI 结果和教师最终结果在同一卡片内连续复核。", "Review the question, student response, SmarTAI result, and teacher result in one card.")}</p>
        </div>
        <QuestionButtons locale={locale} previous={previous} next={next} onSelect={onNavigate} />
      </header>

      <div className="p-5">
        <div className="grid gap-4 xl:grid-cols-2">
          <ContentBlock title={tx(locale, "题目", "Question")}>
            {question.stem ? <MarkdownMath className="text-[13px] leading-6 text-foreground">{question.stem}</MarkdownMath> : <EmptyText locale={locale} text="未提供题干。" en="No question stem was provided." />}
          </ContentBlock>
          <ContentBlock title={tx(locale, "学生作答", "Student Response")} meta={`${student.id} · ${question.label}`}>
            {answer?.content ? <MarkdownMath className="text-[13px] leading-6 text-foreground">{answer.content}</MarkdownMath> : <EmptyText locale={locale} text="未识别到这道题的作答。" en="No response was recognized for this question." />}
          </ContentBlock>
        </div>

        <div className="mt-4 grid items-stretch gap-4 xl:grid-cols-2">
          <section className="rounded-[9px] border bg-background px-5 py-4" aria-labelledby={`smartai-result-${question.id}`}>
            <div className="flex items-center justify-between gap-3">
              <h3 id={`smartai-result-${question.id}`} className="text-[16px] font-bold text-foreground">{tx(locale, "SmarTAI 批改结果", "SmarTAI Grading Result")}</h3>
              <span className="shrink-0 rounded-full bg-blue-50 px-3 py-1 text-xs font-semibold text-primary dark:bg-blue-950/50">
                {correction
                  ? displayScore === null
                    ? tx(locale, "待教师复核", "Teacher review required")
                    : `${formatScore(displayScore)} / ${formatScore(correction.max_score)}`
                  : "— / —"}
              </span>
            </div>
            {correction ? (
              <>
                {hideAutomatedScores ? (
                  <p className="mt-3 rounded-[8px] bg-amber-50 px-3 py-2.5 text-[12px] leading-5 text-amber-800 dark:bg-amber-950/35 dark:text-amber-100">
                    {tx(locale, "为避免把低置信或存在分歧的 AI 判断误当成正式分数，AI 暂定分、评语和评分细节已隐藏。请结合题目、作答、评分标准和下方复核原因判断。", "The provisional score, AI rationale, and score details are hidden so a low-confidence or disputed AI judgment is not mistaken for a formal grade. Review the question, response, rubric, and reasons below.")}
                  </p>
                ) : (
                  <MarkdownMath className="mt-3 text-[13px] leading-6 text-foreground">{correction.comment || tx(locale, "SmarTAI 未返回文字说明。", "SmarTAI returned no written rationale.")}</MarkdownMath>
                )}
                {reviewReasons.length ? <div className="mt-3 flex flex-wrap gap-1.5">{reviewReasons.map((reason) => <span key={reason} className="rounded-full bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-200">{reason}</span>)}</div> : null}
                {!hideAutomatedScores && correction.steps?.length ? (
                  <div className="mt-4 border-t pt-3">
                    <p className="text-[11px] font-semibold text-muted-foreground">{tx(locale, "评分步骤", "Scoring steps")}</p>
                    <ol className="mt-2 space-y-2">
                      {correction.steps.map((step) => (
                        <li key={`${question.id}-${step.step_no}`} className="grid grid-cols-[24px_minmax(0,1fr)_auto] gap-2 rounded-[7px] bg-muted/55 px-3 py-2 text-[11px] leading-5">
                          <span className="font-semibold text-muted-foreground">{step.step_no}</span>
                          <span className="text-foreground">{step.desc}</span>
                          <span className={cn("font-semibold", step.is_correct ? "text-emerald-600" : "text-amber-600")}>{formatScore(step.score)} {tx(locale, "分", "pts")}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : null}
                {!hideAutomatedScores ? (
                  <dl className="mt-4 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                    <Signal label={tx(locale, "置信度", "Confidence")} value={formatConfidence(correction.confidence)} />
                    <Signal label={tx(locale, "专家数", "Models")} value={String(Math.max(1, correction.expert_results?.length ?? 0))} />
                    <Signal label={tx(locale, "合成方式", "Synthesis")} value={formatSynthesis(correction.synthesis_method, locale)} />
                  </dl>
                ) : null}
                {!hideAutomatedScores && correction.expert_results?.length ? (
                  <details className="mt-3 text-xs text-muted-foreground">
                    <summary className="cursor-pointer font-semibold text-foreground">{tx(locale, "查看各专家原始结果", "View Original Model Results")}</summary>
                    <ul className="mt-2 space-y-2">
                      {correction.expert_results.map((expert, index) => (
                        <li key={`${expert.provider}-${index}`} className="rounded-md bg-muted/60 px-3 py-2">
                          <p className="font-semibold text-foreground">{expert.provider} · {formatScore(expert.score)}/{formatScore(expert.max_score)} · {formatConfidence(expert.confidence)}</p>
                          {expert.comment ? <MarkdownMath className="mt-1 text-[11px] leading-5">{expert.comment}</MarkdownMath> : null}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </>
            ) : <EmptyText locale={locale} text="这道题没有可复核的批改结果。" en="No grading result is available for this question." />}
          </section>

          <section className="flex min-h-[300px] flex-col rounded-[9px] border bg-background px-5 py-4" aria-labelledby={`teacher-result-${question.id}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <h3 id={`teacher-result-${question.id}`} className="text-[16px] font-bold text-foreground">{tx(locale, "教师最终结果", "Teacher Final Result")}</h3>
                <ReviewStatus correction={correction} required={required} locale={locale} />
              </div>
              <label className="flex shrink-0 items-center gap-2 text-[11px] font-medium text-muted-foreground">
                <span>{tx(locale, "最终得分", "Final score")}</span>
                <span className="flex h-9 items-center rounded-[8px] border bg-card focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/15">
                  <input
                    value={draft.score}
                    onChange={(event) => onDraftChange({ score: event.target.value })}
                    inputMode="decimal"
                    aria-invalid={Boolean(scoreError)}
                    disabled={!correction || saving}
                    className="w-[58px] bg-transparent px-2 text-right text-sm font-semibold text-foreground outline-none disabled:opacity-50"
                    aria-label={tx(locale, `${question.label} 最终得分`, `${question.label} final score`)}
                  />
                  <span className="border-l px-2 text-xs font-semibold text-foreground">/ {correction ? formatScore(correction.max_score) : "—"}</span>
                </span>
              </label>
            </div>
            {scoreError ? <p className="mt-1 text-right text-xs font-medium text-red-500">{scoreError}</p> : null}
            <label className="mt-4 grid gap-1.5 text-xs font-medium text-muted-foreground">
              {tx(locale, "补充评语（可选）", "Additional comment (optional)")}
              <textarea
                value={draft.comment}
                onChange={(event) => onDraftChange({ comment: event.target.value })}
                rows={5}
                maxLength={4000}
                disabled={!correction || saving}
                placeholder={tx(locale, "记录改分依据，或补充给学生的反馈。", "Explain an override or add feedback for the student.")}
                className="min-h-[126px] resize-y rounded-[8px] border bg-card px-3 py-2.5 text-sm leading-6 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:opacity-50"
              />
            </label>
            {saveError ? <p className="mt-3 rounded-[8px] border border-red-200 bg-red-50 px-3 py-2 text-xs font-medium text-red-700" role="alert">{saveError}</p> : null}
            <div className="mt-auto flex flex-wrap items-center justify-end gap-2 pt-4">
              <button type="button" onClick={onConfirm} disabled={!correction || saving} aria-label={`${question.label}：${actionLabel}`} className="inline-flex h-9 items-center gap-1.5 rounded-[8px] bg-primary px-4 text-xs font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
                {saving ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : <Check aria-hidden="true" className="h-3.5 w-3.5" />}
                {actionLabel}
                {hasNextReview ? <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" /> : null}
              </button>
            </div>
          </section>
        </div>

        <section className="mt-4 rounded-[9px] bg-muted/45 px-4 py-3" aria-label={tx(locale, "评分标准", "Rubric")}>
          <h3 className="text-[12px] font-bold text-foreground">{tx(locale, "评分标准", "Rubric")}</h3>
          {question.criterion ? <MarkdownMath className="mt-1 text-[12px] leading-5 text-muted-foreground">{question.criterion}</MarkdownMath> : <EmptyText locale={locale} text="未提供评分标准。" en="No rubric was provided." />}
        </section>
      </div>
    </article>
  );
}

function ContentBlock({ title, meta, children }: { title: string; meta?: string; children: ReactNode }) {
  return (
    <section className="min-h-[112px] rounded-[9px] bg-muted/45 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[12px] font-bold text-foreground">{title}</h3>
        {meta ? <span className="truncate text-[10px] text-muted-foreground">{meta}</span> : null}
      </div>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function NavButton({ className, disabled, onClick, icon: Icon, label, iconAfter = false }: { className?: string; disabled: boolean; onClick: () => void; icon: typeof ArrowLeft; label: string; iconAfter?: boolean }) {
  return (
    <button type="button" disabled={disabled} onClick={onClick} className={cn("inline-flex h-10 min-w-0 items-center justify-center gap-1.5 rounded-[7px] px-3 text-[12px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-35", className)}>
      {!iconAfter ? <Icon aria-hidden="true" className="h-4 w-4 shrink-0" /> : null}
      <span className="truncate">{label}</span>
      {iconAfter ? <Icon aria-hidden="true" className="h-4 w-4 shrink-0" /> : null}
    </button>
  );
}

function QuestionButtons({ locale, previous, next, onSelect }: { locale: Locale; previous: QuestionSummary | null; next: QuestionSummary | null; onSelect: (id: string) => void }) {
  return (
    <div className="flex shrink-0 gap-2">
      <button type="button" disabled={!previous} onClick={() => previous && onSelect(previous.id)} className="inline-flex h-9 items-center justify-center gap-1.5 rounded-[7px] border bg-card px-3 text-xs font-semibold text-foreground hover:bg-muted disabled:opacity-35"><ArrowUp aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "上一题", "Previous")}</button>
      <button type="button" disabled={!next} onClick={() => next && onSelect(next.id)} className="inline-flex h-9 items-center justify-center gap-1.5 rounded-[7px] border bg-card px-3 text-xs font-semibold text-foreground hover:bg-muted disabled:opacity-35">{tx(locale, "下一题", "Next")}<ArrowDown aria-hidden="true" className="h-3.5 w-3.5" /></button>
    </div>
  );
}

function Signal({ label, value }: { label: string; value: string }) {
  return <div className="rounded-[7px] bg-muted/60 px-3 py-2"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 truncate font-semibold text-foreground" title={value}>{value}</dd></div>;
}

function ReviewStatus({ correction, required, locale }: { correction?: Correction; required: boolean; locale: Locale }) {
  const source = correction ? correctionScoreSource(correction) : null;
  const status = !correction
    ? "missing"
    : source === "hard_failure"
      ? "hard"
      : source === "teacher_confirmed_same"
        ? "confirmed"
        : source === "teacher_changed"
          ? "edited"
        : required
          ? "pending"
          : "ready";
  const label = status === "missing"
    ? tx(locale, "无批改结果", "Missing result")
    : status === "hard"
      ? tx(locale, "无有效分数 · 必须处理", "No valid score · action required")
    : status === "confirmed"
      ? tx(locale, "教师已处理 · 沿用 AI 分", "Teacher handled · AI score retained")
      : status === "edited"
        ? tx(locale, "教师已修改", "Teacher changed")
        : status === "pending"
          ? tx(locale, "AI 分默认采用 · 教师未操作", "AI score used by default · no teacher action")
          : tx(locale, "AI 自动评分 · 教师未操作", "AI scored · no teacher action");
  return <span className={cn("rounded-full px-3 py-1 text-[11px] font-semibold", status === "confirmed" && "bg-blue-100 text-primary dark:bg-blue-950/60", status === "edited" && "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-200", status === "pending" && "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-200", status === "ready" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-200", (status === "missing" || status === "hard") && "bg-red-100 text-red-600 dark:bg-red-950/60 dark:text-red-200")}>{label}</span>;
}

function EmptyText({ locale, text, en }: { locale: Locale; text: string; en: string }) {
  return <p className="text-[12px] leading-5 text-muted-foreground">{tx(locale, text, en)}</p>;
}

function PageState({ title, busy = false, action, href, onAction }: { title: string; busy?: boolean; action?: string; href?: string; onAction?: () => void }) {
  const button = action && href ? <Link to={href} className="mt-4 inline-flex h-9 items-center rounded-[8px] bg-primary px-4 text-xs font-semibold text-primary-foreground">{action}</Link> : action && onAction ? <button type="button" onClick={onAction} className="mt-4 inline-flex h-9 items-center rounded-[8px] bg-primary px-4 text-xs font-semibold text-primary-foreground">{action}</button> : null;
  return <section className="mt-5 flex min-h-[260px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center" aria-busy={busy || undefined}>{busy ? <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" /> : <AlertTriangle aria-hidden="true" className="h-7 w-7 text-muted-foreground" />}<p className="mt-3 text-sm font-semibold text-foreground">{title}</p>{button}</section>;
}

function reviewQuestionState(correction: Correction | undefined, required: boolean): ResultQuestionState {
  if (!correction) return "danger";
  if (effectiveCorrectionScore(correction) === null) return "danger";
  const source = correctionScoreSource(correction);
  if (source === "teacher_confirmed_same" || source === "teacher_changed") return "confirmed";
  if (required) return "warning";
  return "ready";
}

function formatSynthesis(value: string | null | undefined, locale: Locale) {
  const labels: Record<string, [string, string]> = { single: ["单专家", "Single"], median: ["中位数", "Median"], multi_sample: ["多次采样", "Multi-sample"], weighted_average: ["加权平均", "Weighted"], judge_agent: ["裁判模型", "Judge"], degraded_to_single: ["降级单专家", "Fallback"], all_failed: ["全部失败", "Failed"], quota_exhausted: ["额度耗尽", "Quota"] };
  const pair = value ? labels[value] : null;
  return pair ? (locale === "en-US" ? pair[1] : pair[0]) : (value || "—");
}

function omitKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

function isInteractiveTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest("input, textarea, select, button, a, summary, [contenteditable='true'], [role='dialog']"));
}

function questionAnchorId(questionId: string) {
  return `review-question-${questionId}`;
}

function tx(locale: Locale, zh: string, en: string) {
  return locale === "en-US" ? en : zh;
}

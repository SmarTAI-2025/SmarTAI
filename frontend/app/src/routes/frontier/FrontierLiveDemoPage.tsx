import { isTaskAwaitingGrading, isTaskPaused } from "@/lib/taskFlow";
import { TASK_ACTION_KEYS, TASK_STAGE_KEYS } from "@/lib/taskPresentation";
import { QUESTION_FIXTURE, SUBMISSION_FIXTURE, DEMO_SCORE_POLICY, fixtureFile } from "@/lib/frontierFixtures";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { useGradingIntent } from "@/hooks/useGradingIntent";
import { getCurrentSemesterId } from "@/lib/semesters";
import {
  Activity,
  ArrowRight,
  BarChart3,
  Check,
  CircleDot,
  Clock3,
  ExternalLink,
  FileText,
  FlaskConical,
  LoaderCircle,
  Pause,
  Play,
  RotateCcw,
  ScanText,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { preflightProblemSource, startQuestionPreparation } from "@/api/problemSources";
import { contentLanguageFromLocale, initializeContentLanguage, type ContentLanguage } from "@/api/contentLanguage";
import {
  createTask,
  getTask,
  getTaskResult,
  getTaskState,
  parseSubmissions,
  retryQuestionPreparation,
  retrySubmissionRecognition,
  startGrading,
} from "@/api/tasks";
import { SmarTAIAppMark, SmarTAIWordmark } from "@/components/brand/SmarTAIBrand";
import { Button } from "@/components/ui/Button";
import { FrontierExitLink } from "@/components/auth/FrontierExitLink";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { SyntaxHighlightedCode } from "@/components/ui/SyntaxHighlightedCode";
import { PdfDocumentPreview } from "@/components/tasks/PdfDocumentPreview";
import { recognitionEventText } from "@/components/tasks/RecognitionActivity";
import { gradingActivityText } from "@/components/tasks/GradingActivity";
import { buildResultsModel, formatPercent, formatScore, problemLabel } from "@/components/tasks/resultsModel";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { getFrontierGeneration, isFrontierExitPending, isFrontierTaskPath, rememberFrontierTask } from "@/lib/frontierSession";
import { answerReviewNextBlocked, submissionReviewBlockers, questionReviewBlockers, type ReviewBlocker } from "@/lib/reviewConfirmation";
import { formatSubmissionFlag } from "@/lib/submissionReview";
import { gradingProgressText } from "@/lib/gradingProgressCopy";
import "./frontier.css";
import type { ProblemInfo, Task, TaskResultResponse, TaskStateSnapshot, TaskStatus } from "@/types";

const HANDWRITTEN_FIXTURE = "/frontier-demo/live/DEMO-002_handwritten_raw.png";
const POLL_INTERVAL_MS = 1_500;
const EXPECTED_DEMO_QUESTION_COUNT = 4;

type RunStepId = "task" | "questions" | "submissions" | "grading";
type RunStepState = "waiting" | "active" | "complete" | "error";

interface DemoRun {
  contentLanguage?: ContentLanguage;
  taskId: string | null;
  isCurrent: () => boolean;
  step: <T>(operation: () => Promise<T>) => Promise<T>;
}

interface RunStep {
  id: RunStepId;
  label: string;
  detail: string;
  state: RunStepState;
  jobId?: string | null;
}

function initialSteps(locale: Locale): RunStep[] {
  return [
    { id: "task", label: tx(locale, "创建专属 Demo 任务", "Create a dedicated demo task"), detail: tx(locale, "准备演示任务", "Prepare the demo task"), state: "waiting" },
    { id: "questions", label: tx(locale, "准备并审核题目", "Prepare and review questions"), detail: tx(locale, "识别题目并生成标答与评分标准", "Recognize questions and prepare answers and rubrics"), state: "waiting" },
    { id: "submissions", label: tx(locale, "识别混合作答", "Recognize mixed submissions"), detail: tx(locale, "识别 PDF 和图片作答", "Recognize PDF and image submissions"), state: "waiting" },
    { id: "grading", label: tx(locale, "运行 AI 批改", "Run AI grading"), detail: tx(locale, "按评分设置批改", "Grade using the selected settings"), state: "waiting" },
  ];
}

export function FrontierLiveDemoPage() {
  const { locale, t } = useI18n();
  const navigate = useNavigate();
  const gradingIntent = useGradingIntent();
  const [searchParams] = useSearchParams();
  const taskIdFromUrl = safeTaskId(searchParams.get("taskId"));
  const [taskId, setTaskId] = useState<string | null>(taskIdFromUrl);
  const [snapshot, setSnapshot] = useState<TaskStateSnapshot | null>(null);
  const [steps, setSteps] = useState<RunStep[]>(() => initialSteps(locale));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; code?: string | null } | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [providerLabel, setProviderLabel] = useState<string | null>(null);
  const [teacherReviewTask, setTeacherReviewTask] = useState<Task | null>(null);
  const [submissionReviewTask, setSubmissionReviewTask] = useState<Task | null>(null);
  const [demoResult, setDemoResult] = useState<TaskResultResponse | null>(null);
  const [materialsConfirmed, setMaterialsConfirmed] = useState(false);
  const activeStepRef = useRef<RunStepId | null>(null);
  const runRef = useRef<DemoRun | null>(null);
  const localeRef = useRef(locale);
  localeRef.current = locale;
  // Only completed recognition can require manual review. Failed/paused jobs
  // retain their existing retry/resume action, even if partial data is present.
  const reviewTask = isTaskAwaitingGrading(snapshot?.status) ? submissionReviewTask
    : snapshot?.status === "problems_ready" ? teacherReviewTask : null;
  const questionBlockers = reviewTask ? demoQuestionReviewBlockers(reviewTask, locale) : [];
  const submissionBlockers = reviewTask && isTaskAwaitingGrading(snapshot?.status) ? submissionReviewBlockers(reviewTask, locale) : [];
  const requiredReview = !error && reviewTask && (questionBlockers.length || submissionBlockers.length) ? {
    href: `/tasks/${reviewTask.task_id}/${questionBlockers.length ? "questions" : "submissions"}`,
    label: questionBlockers.length ? tx(locale, "审核题目", "Review questions") : tx(locale, "校对作答", "Review submissions"),
    description: questionBlockers.length
      ? (isTaskAwaitingGrading(snapshot?.status)
        ? tx(locale, "请审核并修正题目中的问题，再开始批改。", "Review and correct the questions before grading.")
        : tx(locale, "请审核并修正题目中的问题，再继续识别作答。", "Review and correct the questions before recognizing submissions."))
      : tx(locale, "请修正学生信息或作答中的问题，再开始批改。", "Correct the student details or answers before grading."),
  } : null;

  useEffect(() => () => { runRef.current = null; }, []);

  function beginRun(currentTaskId: string | null): DemoRun | null {
    if (runRef.current || isFrontierExitPending()) return null;
    const generation = getFrontierGeneration();
    const run: DemoRun = {
      taskId: currentTaskId,
      isCurrent: () => runRef.current === run && generation === getFrontierGeneration() && !isFrontierExitPending(),
      async step(operation) {
        // Check both sides of every async boundary: a successful response can
        // arrive after unmount, a task switch, or confirmed session exit.
        if (!run.isCurrent()) throw new Error("Demo run is no longer active.");
        const result = await operation();
        if (!run.isCurrent()) throw new Error("Demo run is no longer active.");
        return result;
      },
    };
    runRef.current = run;
    setBusy(true);
    return run;
  }

  function finishRun(run: DemoRun) {
    // A late finally from an old task must never unlock its replacement.
    if (runRef.current !== run) return;
    runRef.current = null;
    setBusy(false);
  }

  useEffect(() => {
    // A newly created task adopts its URL without starting a second workflow.
    if (runRef.current && runRef.current.taskId === taskIdFromUrl) return;
    runRef.current = null;
    setBusy(false);
    setTaskId(taskIdFromUrl);
    setSnapshot(null);
    setTeacherReviewTask(null);
    setSubmissionReviewTask(null);
    setDemoResult(null);
    setMaterialsConfirmed(false);
    setProviderLabel(null);
    setError(null);
    setStartedAt(null);
    activeStepRef.current = null;
    const locale = localeRef.current;
    setSteps(initialSteps(locale));
    if (!taskIdFromUrl) return;
    const restoredTaskId = taskIdFromUrl;
    const run = beginRun(restoredTaskId);
    if (!run) return;
    async function restoreCurrentTask(run: DemoRun) {
      try {
        const state = await run.step(() => getTaskState(restoredTaskId));
        setTaskId(restoredTaskId);
        setSnapshot(state);
        setSteps((current) => current.some((step) => step.state === "error") ? current : stepsFromSnapshot(state, locale));
        if (isTaskPaused(state.status)) return;
        if (state.status === "error") {
          const detail = state.progress?.error_detail || state.error || "workflow_error";
          setError({ message: liveDemoWorkflowFailureMessage(locale, detail), code: detail });
          return;
        }
        if (state.problem_count > 0 || state.status === "problems_ready" || isTaskAwaitingGrading(state.status)) {
          let task = await run.step(() => getTask(restoredTaskId));
          if (state.status === "problems_ready" || isTaskAwaitingGrading(state.status)) {
            const reviewed = await readCurrentReview(run, restoredTaskId, state.status === "problems_ready" ? "questions" : "submissions", { task, state });
            if (!reviewed) return;
            task = reviewed.task;
          }
          const prepared = Object.values(task.problem_data);
          const confirmed = generatedMaterialsConfirmed(prepared);
          setTeacherReviewTask(task);
          setMaterialsConfirmed(confirmed);
          if (state.status === "problems_ready" && !confirmed) {
            return;
          }
          if (isTaskAwaitingGrading(state.status)) {
            setSubmissionReviewTask(task);
            return;
          }
        }
        if (["graded", "finalized"].includes(state.status)) {
          setDemoResult(await run.step(() => getTaskResult(restoredTaskId)));
          return;
        }
        if (["extracting_problems", "parsing_submissions", "grading"].includes(state.status)) {
          setError(null);
          setStartedAt(Date.now());
          await continueWorkflow(run, restoredTaskId, state.status);
        }
      } catch (caught) {
        if (run.isCurrent()) {
          failActiveStep();
          setError(errorMessage(caught));
        }
      } finally {
        finishRun(run);
      }
    }
    void restoreCurrentTask(run);
    // Display-language changes do not cancel or restart backend work.
  }, [taskIdFromUrl]);

  useEffect(() => {
    setSteps((current) => {
      const translated = snapshot ? stepsFromSnapshot(snapshot, locale) : initialSteps(locale);
      if (!busy && !current.some((step) => step.state === "error")) return translated;
      // Keep the active/failed step without freezing its previous language.
      return translated.map((step) => {
        const previous = current.find((item) => item.id === step.id);
        return { ...step, state: previous?.state ?? step.state, jobId: previous?.jobId ?? step.jobId };
      });
    });
  }, [busy, locale, snapshot]);

  const elapsed = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1_000)) : null;
  useEffect(() => {
    if (!taskIdFromUrl && searchParams.get("workflow") === "full") void startFreshRun(true);
  }, [taskIdFromUrl, searchParams.get("workflow")]);

  async function retryFailedItems() {
    if (!taskId) return startFreshRun();
    if (submissionReviewTask && isTaskAwaitingGrading(snapshot?.status)) return confirmSubmissionReview();
    const run = beginRun(taskId);
    if (!run) return;
    const previousError = error;
    setError(null);
    try {
      const current = await run.step(() => getTaskState(taskId));
      setSnapshot(current);
      let status = current.status;
      if (status === "error" || isTaskPaused(status)) {
        const failedJobId = current.last_failed_job_id;
        if (current.grading_job_id && (failedJobId === current.grading_job_id || status === "grading_paused")) {
          const response = await run.step(() => gradingIntent.execute(current.workflow_revision, request => startGrading(taskId, { ...request, retryScope: "failed_only" })));
          if (!response) return;
          status = "grading";
        } else {
          const submissions = current.parse_job_id && (failedJobId === current.parse_job_id || status === "submissions_paused");
          const jobId = submissions ? current.parse_job_id : current.extract_job_id && (failedJobId === current.extract_job_id || status === "problems_paused") ? current.extract_job_id : null;
          if (!jobId) throw new Error(current.error || previousError?.message || "workflow_error");
          const recognitionProviderId = await run.step(() => demoRecognitionProvider(taskId));
          await run.step(() => (submissions ? retrySubmissionRecognition : retryQuestionPreparation)({
            taskId, jobId, recognitionProviderId, expectedWorkflowRevision: current.workflow_revision,
            // Quick Demo retries may repeat an uncertain call against the Demo quota.
            acknowledgePossibleDuplicateCall: true,
          }));
          status = submissions ? "parsing_submissions" : "extracting_problems";
        }
      }
      await continueWorkflow(run, taskId, status);
    } catch (caught) {
      if (run.isCurrent()) {
        failActiveStep();
        setError(errorMessage(caught));
      }
    } finally { finishRun(run); }
  }

  async function startFreshRun(fullWorkflow = false) {
    const run = beginRun(null);
    if (!run) return;
    setError(null);
    setSnapshot(null);
    setProviderLabel(null);
    setTeacherReviewTask(null);
    setSubmissionReviewTask(null);
    setDemoResult(null);
    setMaterialsConfirmed(false);
    setStartedAt(Date.now());
    setSteps(initialSteps(locale));
    activeStepRef.current = null;
    try {
      markStep("task", "active", tx(locale, "正在创建任务…", "Creating the task…"));
      const task = await run.step(() => createTask({
        authScope: "frontier",
        semester_id: getCurrentSemesterId(),
        name: `SmarTAI Live Demo · ${new Date().toISOString().slice(0, 16).replace("T", " ")} UTC`,
        idempotencyKey: createIdempotencyKey(),
      }));
      rememberFrontierTask(task.task_id);
      run.taskId = task.task_id;
      setTaskId(task.task_id);
      markStep("task", "complete", tx(locale, "任务已创建", "Task created"));
      // Keep the created task reachable if saving its language needs a retry.
      navigate(`/frontier/live?taskId=${encodeURIComponent(task.task_id)}`, { replace: true });
      const language = await run.step(() => initializeContentLanguage(task.task_id, contentLanguageFromLocale(locale)));
      run.contentLanguage = language.language;
      if (fullWorkflow) {
        navigate(`/tasks/${encodeURIComponent(task.task_id)}/edit`, { replace: true });
        return;
      }
      await continueWorkflow(run, task.task_id, task.status);
    } catch (caught) {
      if (run.isCurrent()) {
        failActiveStep();
        setError(errorMessage(caught));
      }
    } finally {
      finishRun(run);
    }
  }

  async function confirmTeacherMaterials() {
    if (!taskId || !teacherReviewTask) return;
    const run = beginRun(taskId);
    if (!run) return;
    setError(null);
    try {
      markStep("questions", "active", tx(locale, "正在检查题目是否可以进入下一阶段…", "Checking that questions are ready for the next stage…"));
      await continueWorkflow(run, taskId, "problems_ready", true);
    } catch (caught) {
      if (run.isCurrent()) {
        failActiveStep();
        setError(errorMessage(caught));
      }
    } finally {
      finishRun(run);
    }
  }

  async function confirmSubmissionReview() {
    if (!taskId || !submissionReviewTask) return;
    const run = beginRun(taskId);
    if (!run) return;
    setError(null);
    try {
      markStep("grading", "active", tx(locale, "正在启动批改…", "Starting grading…"));
      const reviewed = await readCurrentReview(run, taskId, "submissions");
      if (!reviewed) return;
      const current = reviewed.task;
      setSubmissionReviewTask(current);
      const blockers = [...demoQuestionReviewBlockers(current, locale), ...submissionReviewBlockers(current, locale)];
      if (blockers.length) {
        failActiveStep();
        return;
      }
      let setup = await run.step(() => getGradingSetup(taskId));
      if (!setup.grading_setup) {
        const defaults = setup.suggested_setup;
        if (!defaults) throw new Error(tx(locale, "当前 Demo 会话没有可用的批改模型。", "No enabled grading provider is available for this demo session."));
        setup = await run.step(() => saveGradingSetup({
          taskId, expectedWorkflowRevision: setup.workflow_revision, gradingSetup: defaults,
        }));
      }
      const response = await run.step(() => gradingIntent.execute(setup.workflow_revision, request => startGrading(taskId, request)));
      if (!response) return;
      await continueWorkflow(run, taskId, "grading");
    } catch (caught) {
      if (run.isCurrent()) {
        failActiveStep();
        setError(errorMessage(caught));
      }
    } finally {
      finishRun(run);
    }
  }

  async function readCurrentReview(run: DemoRun, currentTaskId: string, stage: "questions" | "submissions", loaded?: { task: Task; state: TaskStateSnapshot }) {
    // Content and workflow reads must describe the same revision and stage.
    // A later state alone must never authorize work against earlier content.
    for (let attempt = 0; attempt < 2; attempt++) {
      const task = loaded?.task ?? await run.step(() => getTask(currentTaskId));
      const state = loaded && task.status === loaded.state.status && task.workflow_revision === loaded.state.workflow_revision
        ? loaded.state : await run.step(() => getTaskState(currentTaskId));
      if (!await reviewStageIsCurrent(run, currentTaskId, stage, state)) return null;
      if (task.status === state.status && task.workflow_revision === state.workflow_revision) return { task, state };
      loaded = undefined;
    }
    throw new Error(tx(locale, "任务内容已更新，请重试读取最新状态。", "Task content changed. Retry to load the latest state."));
  }

  async function reviewStageIsCurrent(run: DemoRun, currentTaskId: string, stage: "questions" | "submissions", state: TaskStateSnapshot) {
    // Another tab can restart recognition while this review remains open.
    // Reconcile persisted workflow state before classifying content as a human blocker.
    setSnapshot(state);
    if (!isTaskAwaitingGrading(state.status)) setSubmissionReviewTask(null);
    if (stage === "questions" ? state.status === "problems_ready" : isTaskAwaitingGrading(state.status)) return true;
    activeStepRef.current = null;
    setSteps(stepsFromSnapshot(state, locale));
    if (state.status === "error") {
      const detail = state.progress?.error_detail || state.error || "workflow_error";
      throw new Error(liveDemoWorkflowFailureMessage(locale, detail));
    }
    if (!isTaskPaused(state.status) && state.status !== "draft") {
      await continueWorkflow(run, currentTaskId, state.status);
    }
    return false;
  }

  async function continueWorkflow(run: DemoRun, currentTaskId: string, startingStatus: TaskStatus, continueToSubmissions = false) {
    let status = startingStatus;

    if (status === "draft") {
      const language = run.contentLanguage ?? (await run.step(() => initializeContentLanguage(currentTaskId, contentLanguageFromLocale(locale)))).language;
      const recognitionProviderId = await run.step(() => demoRecognitionProvider(currentTaskId));
      markStep("questions", "active", tx(locale, "正在上传原始题目并运行完整题目准备…", "Uploading the raw source and running full question preparation…"));
      const questionSource = await run.step(() => fixtureFile(QUESTION_FIXTURE, "question_source.pdf", "application/pdf"));
      const preflight = await run.step(() => preflightProblemSource({
        taskId: currentTaskId,
        mode: "upload",
        role: "problem",
        file: questionSource,
        structureMode: "organized",
        extractionHint: tx(locale, "按 Q1–Q4 保留题号与数学符号。", "Preserve the Q1–Q4 numbering and mathematical notation."),
        saveToLibrary: false,
        recognitionProviderId,
      }));
      const expectedWorkflowRevision = preflight.workflow_revision ?? preflight.base_workflow_revision;
      if (expectedWorkflowRevision === undefined) {
        throw new Error(tx(locale, "题目准备预检未返回工作流版本。", "Question-preparation preflight did not return a workflow revision."));
      }
      const response = await run.step(() => startQuestionPreparation({
        taskId: currentTaskId,
        sourceTokens: [preflight.source_token],
        expectedWorkflowRevision,
        scorePolicy: { mode: "per_question", perQuestionText: DEMO_SCORE_POLICY },
        generationLanguage: language,
        recognitionProviderId,
      }));
      markStep("questions", "active", tx(locale, "正在识别题目并生成标答、评分依据与代码题材料…", "Recognizing questions and generating answers, rubrics, and programming materials…"), response.job_id);
      const state = await waitForStatus(run, currentTaskId, "extracting_problems", ["problems_ready"]);
      if (!state) return;
      status = state.status;
    } else if (status === "extracting_problems") {
      markStep("questions", "active", tx(locale, "正在等待已有题目识别任务…", "Waiting for the existing question-recognition job…"));
      const state = await waitForStatus(run, currentTaskId, "extracting_problems", ["problems_ready"]);
      if (!state) return;
      status = state.status;
    }

    if (status === "problems_ready") {
      const reviewed = await readCurrentReview(run, currentTaskId, "questions");
      if (!reviewed) return;
      const recognizedTask = reviewed.task;
      setTeacherReviewTask(recognizedTask);
      const alreadyConfirmed = generatedMaterialsConfirmed(Object.values(recognizedTask.problem_data));
      setMaterialsConfirmed(alreadyConfirmed);
      // Recheck persisted content on every explicit continuation, including
      // edits made in another tab since the Demo was loaded.
      if (!continueToSubmissions || demoQuestionReviewBlockers(recognizedTask, locale).length) {
        markStep("questions", "complete", tx(locale, "题目、标答与评分资料已生成，可继续识别作答。", "Questions, answers, and rubrics are ready. Continue to submissions."), recognizedTask.extract_job_id);
        return;
      }
      const afterQuestions = reviewed.state;
      markStep("questions", "complete", tx(locale, `已识别 ${afterQuestions.problem_count} 题，继续识别作答`, `${afterQuestions.problem_count} questions recognized; continuing to submissions`), afterQuestions.extract_job_id);

      markStep("submissions", "active", tx(locale, "正在上传原始排版、混排与手写作答…", "Uploading raw typeset, mixed, and handwritten submissions…"));
      const archive = await run.step(() => fixtureFile(SUBMISSION_FIXTURE, "submissions_raw.zip", "application/zip"));
      const recognitionProviderId = await run.step(() => demoRecognitionProvider(currentTaskId));
      const response = await run.step(() => parseSubmissions({ taskId: currentTaskId, file: archive, identityMode: "filename", recognitionProviderId, expectedWorkflowRevision: afterQuestions.workflow_revision }));
      markStep("submissions", "active", tx(locale, "作答识别与 OCR 正在运行…", "Submission recognition and OCR are running…"), response.job_id);
      const state = await waitForStatus(run, currentTaskId, "parsing_submissions", ["submissions_ready", "grading_setup"]);
      if (!state) return;
      status = state.status;
    } else if (status === "parsing_submissions") {
      markStep("questions", "complete", tx(locale, "题目已准备", "Questions already prepared"));
      markStep("submissions", "active", tx(locale, "正在等待已有作答识别任务…", "Waiting for the existing submission-recognition job…"));
      const state = await waitForStatus(run, currentTaskId, "parsing_submissions", ["submissions_ready", "grading_setup"]);
      if (!state) return;
      status = state.status;
    }

    if (isTaskAwaitingGrading(status)) {
      const reviewed = await readCurrentReview(run, currentTaskId, "submissions");
      if (!reviewed) return;
      const recognized = reviewed.state;
      setSubmissionReviewTask(reviewed.task);
      markStep("submissions", "complete", tx(locale, `已识别 ${recognized.student_count} 份作答`, `${recognized.student_count} submissions recognized`), recognized.parse_job_id);
      return;
    } else if (status === "grading") {
      markStep("questions", "complete", tx(locale, "题目已准备", "Questions already prepared"));
      markStep("submissions", "complete", tx(locale, "作答已识别", "Submissions already recognized"));
      markStep("grading", "active", tx(locale, "正在等待已有批改任务…", "Waiting for the existing grading job…"));
      const state = await waitForStatus(run, currentTaskId, "grading", ["graded", "finalized"]);
      if (!state) return;
      status = state.status;
    }

    if (["graded", "finalized"].includes(status)) {
      const completed = await run.step(() => getTaskState(currentTaskId));
      setSnapshot(completed);
      setDemoResult(await run.step(() => getTaskResult(currentTaskId)));
      markStep("grading", "complete", tx(locale, `已处理 ${completed.student_count * completed.problem_count} 题次`, `${completed.student_count * completed.problem_count} responses processed`), completed.grading_job_id);
    }
  }

  async function waitForStatus(run: DemoRun, currentTaskId: string, activeStatus: TaskStatus, targets: TaskStatus[]): Promise<TaskStateSnapshot | null> {
    while (true) {
      const state = await run.step(() => getTaskState(currentTaskId));
      setSnapshot(state);
      if (isTaskPaused(state.status)) {
        setSteps(stepsFromSnapshot(state, locale));
        return state;
      }
      if (targets.includes(state.status)) return state;
      if (state.status === "error") {
        const detail = state.progress?.error_detail || state.error || "workflow_error";
        throw new Error(liveDemoWorkflowFailureMessage(locale, detail));
      }
      if (state.status !== activeStatus) {
        // Another tab may advance or reset the task while this stage is watched.
        // Reconcile through the existing workflow without resuming the old stage.
        activeStepRef.current = null;
        setSteps(stepsFromSnapshot(state, locale));
        setTeacherReviewTask(null);
        setSubmissionReviewTask(null);
        setMaterialsConfirmed(false);
        if (state.status !== "draft") await continueWorkflow(run, currentTaskId, state.status);
        return null;
      }
      await run.step(() => delay(POLL_INTERVAL_MS));
    }
  }

  function markStep(id: RunStepId, state: RunStepState, detail: string, jobId?: string | null) {
    if (state === "active") activeStepRef.current = id;
    else if (activeStepRef.current === id) activeStepRef.current = null;
    setSteps((current) => current.map((step) => step.id === id ? { ...step, state, detail, jobId: jobId ?? step.jobId } : step));
  }

  function failActiveStep() {
    const activeId = activeStepRef.current;
    activeStepRef.current = null;
    setSteps((current) => current.map((step) => step.id === activeId || (!activeId && step.state === "active") ? { ...step, state: "error" } : step));
  }

  return (
    <div className="mx-auto min-w-0 w-full max-w-[1240px] pb-12">
      <div className="flex flex-col gap-6 overflow-hidden rounded-[18px] border border-white/10 bg-[radial-gradient(circle_at_12%_20%,rgba(72,159,236,0.2),transparent_22rem),linear-gradient(125deg,#071426,#10243a_66%,#17202a)] p-6 text-white shadow-[0_24px_55px_rgba(7,20,38,0.2)] sm:p-8 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <FrontierExitLink to="/" aria-label={tx(locale, "返回 SmarTAI 产品介绍", "Back to SmarTAI product overview")} className="inline-flex items-center gap-3 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-white/60">
            <span className="inline-flex h-12 w-12 shrink-0 overflow-hidden rounded-[13px] border border-white/15 bg-[#061225] shadow-lg" aria-hidden="true"><SmarTAIAppMark finish="silver" className="h-full w-full" /></span>
            <SmarTAIWordmark tone="white" alt="" className="w-[156px] sm:w-[178px]" />
            <span className="hidden border-l border-white/20 pl-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-300 sm:inline">{tx(locale, "产品介绍", "Product overview")}</span>
          </FrontierExitLink>
          <h1 className="mt-5 text-[34px] font-bold leading-tight tracking-[-0.035em] text-white sm:text-[42px]">{tx(locale, "真实批改运行", "Live grading run")}</h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-300 sm:text-base">
            {tx(locale, "使用示例作业体验题目识别、作答校对、批改与分析。", "Try question recognition, submission review, grading, and analysis with sample coursework.")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <BoundaryBadge icon={<FlaskConical />} label={tx(locale, "演示样例", "Demo samples")} inverse />
          <BoundaryBadge icon={<Activity />} label={tx(locale, "任务流程", "Task workflow")} tone="blue" inverse />
          <BoundaryBadge icon={<ScanText />} label={tx(locale, "文字识别", "Text recognition")} tone="blue" inverse />
          <BoundaryBadge icon={<Sparkles />} label={tx(locale, "智能批改", "AI grading")} tone="blue" inverse />
        </div>
      </div>


      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(330px,0.9fr)]">
        <section className="flex h-full flex-col rounded-[12px] border bg-card p-5 shadow-sm sm:p-7">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "演示流程", "Demo workflow")}</p>
              <h2 className="mt-2 text-2xl font-bold tracking-[-0.025em]">{tx(locale, "运行完整样例任务", "Run the complete sample task")}</h2>
              <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{tx(locale, "每次运行会创建新任务并使用模型额度。", "Each run creates a new task and uses model quota.")}</p>
            </div>
            {snapshot ? <StatusPill status={snapshot.status} /> : null}
          </div>

          <ol className="mt-7 grid flex-1 grid-rows-4 gap-3">
            {steps.map((step, index) => <WorkflowStep key={step.id} step={step} index={index} locale={locale} />)}
          </ol>

          {requiredReview ? (
            <RecoverableActionState locale={locale} className="mt-5"
              info={{ title: tx(locale, "需要人工校对", "Manual review required"), description: requiredReview.description,
                tone: "danger", actionKind: "reselect", actionLabel: requiredReview.label, technicalDetails: [] }}
              primaryAction={{ label: requiredReview.label, href: requiredReview.href, busy }}
            />
          ) : error ? (
            <RecoverableActionState locale={locale} className="mt-5"
              info={{
                ...classifyRecoverableError(error.code ?? snapshot?.error ?? error.message, { locale, taskId: taskId ?? undefined, jobId: snapshot?.grading_job_id ?? undefined, phase: snapshot?.progress?.current_step ?? undefined }),
              }}
              primaryAction={{ label: tx(locale, "重试失败项", "Retry failed items"), onClick: () => void retryFailedItems(), busy }}
            />
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            {!error && snapshot && isTaskPaused(snapshot.status) ? (
              <Button onClick={() => void retryFailedItems()} disabled={busy}>
                {t(TASK_ACTION_KEYS[snapshot.status])}
              </Button>
            ) : !error && !requiredReview && teacherReviewTask && !submissionReviewTask && snapshot?.status === "problems_ready" ? (
              <Button className="h-12 px-6 text-sm shadow-sm" onClick={() => void confirmTeacherMaterials()} disabled={busy}>
                {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                {tx(locale, "继续识别作答", "Continue to submissions")}
              </Button>
            ) : !error && !requiredReview && submissionReviewTask && isTaskAwaitingGrading(snapshot?.status) ? (
              <Button className="h-12 px-6 text-sm shadow-sm" onClick={() => void confirmSubmissionReview()} disabled={busy}>
                {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                {tx(locale, "开始批改", "Start grading")}
              </Button>
            ) : !error && (!taskId || snapshot?.status === "draft") ? (
              <Button className="h-12 px-6 text-sm shadow-sm" onClick={() => { if (taskId && snapshot?.status === "draft") { const run = beginRun(taskId); if (run) void continueWorkflow(run, taskId, "draft").catch(e => { if (run.isCurrent()) setError(errorMessage(e)); }).finally(() => finishRun(run)); } else void startFreshRun(); }} disabled={busy}>
                {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {busy ? tx(locale, "正在运行真实流程…", "Running real workflow…") : tx(locale, "开始真实 OCR + 批改", "Start real OCR + grading")}
              </Button>
            ) : null}
            {taskId ? <TaskLinks taskId={taskId} status={snapshot?.status} locale={locale} /> : null}
            {taskId ? (
              <Button variant="secondary" className="h-9 px-3 text-xs" onClick={() => void startFreshRun()} disabled={busy}>
                {busy ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                {busy ? tx(locale, "正在运行…", "Running…") : tx(locale, "重新开始", "Start over")}
              </Button>
            ) : null}
          </div>

          <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
            {!taskId ? <span>{tx(locale, "尚未创建任务", "No task created yet")}</span> : null}
            {providerLabel ? <span>{tx(locale, "模型", "provider")} {providerLabel}</span> : null}
            {elapsed !== null ? <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{tx(locale, `已用时 ${elapsed} 秒`, `${elapsed}s elapsed`)}</span> : null}
          </div>

          {snapshot?.progress ? (
            <div className="mt-5 rounded-[9px] border bg-muted/30 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-semibold">
                <span className="inline-flex items-center gap-2 text-foreground"><Activity className="h-4 w-4 text-primary" />{tx(locale, "处理进度", "Progress")}</span>
                <span className="text-muted-foreground">{t(TASK_STAGE_KEYS[snapshot.status])}</span>
              </div>
              <div role="progressbar" aria-label={tx(locale, "当前处理进度", "Current processing progress")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressPercent(snapshot)} className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
                <span className="block h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${progressPercent(snapshot)}%` }} />
              </div>
              <p role="status" aria-live="polite" className="mt-2 text-xs leading-5 text-muted-foreground">{latestProgressMessage(snapshot, locale)}</p>
            </div>
          ) : null}

          {snapshot?.active_operation_status === "pending" ? (
            <p role="status" aria-live="polite" className="mt-2 text-xs leading-5 text-muted-foreground">
              {tx(locale, "当前正在排队，请耐心等待；也可保留此标签页，稍后查看。", "You’re currently in the queue. Please wait, or keep this tab open and check back later.")}
            </p>
          ) : null}
        </section>

        <aside className="grid content-start gap-4">
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><FileText className="h-4 w-4 text-primary" />{tx(locale, "原始题目样例", "Raw question fixture")}</div>
              <a href={QUESTION_FIXTURE} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">{tx(locale, "打开", "Open")} <ExternalLink className="h-3.5 w-3.5" /></a>
            </div>
            <div className="flex h-[310px] items-center justify-center overflow-hidden bg-slate-200">
              <PdfDocumentPreview
                url={QUESTION_FIXTURE}
                title="Synthetic raw question source"
                loadingLabel={tx(locale, "正在载入原始题目…", "Loading raw questions…")}
                errorTitle={tx(locale, "暂时无法显示题目 PDF", "The question PDF could not be displayed")}
                errorDescription={tx(locale, "可重试渲染，或在新窗口打开原文件。", "Retry the preview or open the original in a new window.")}
                retryLabel={tx(locale, "重新载入", "Retry")}
                openLabel={tx(locale, "打开原文件", "Open original")}
              />
            </div>
          </section>
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><ScanText className="h-4 w-4 text-primary" />{tx(locale, "原始手写样例", "Raw handwritten fixture")}</div>
              <span className="rounded-full bg-warning/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-warning">{tx(locale, "OCR 输入", "OCR input")}</span>
            </div>
            <div className="flex h-[280px] items-start justify-center overflow-auto bg-slate-100 p-3">
              <img src={HANDWRITTEN_FIXTURE} alt="Raw synthetic handwritten student submission without grades or teacher annotations" className="max-w-full bg-white shadow-md" />
            </div>
            <p className="border-t px-4 py-3 text-xs leading-5 text-muted-foreground">{tx(locale, "手写作答示例。", "Handwritten submission sample.")}</p>
          </section>
          <InlineNotice tone="warning" title={tx(locale, "检查与复核", "Check and review")}>
            {tx(locale, "可查看题目、作答和批改结果。提示需要处理时，请先完成校对。", "Review questions, submissions, and grading results. Resolve any required corrections before continuing.")}
          </InlineNotice>
        </aside>
      </div>

      {demoResult ? (
        <DemoResultInsights result={demoResult} taskId={taskId ?? demoResult.task_id} locale={locale} />
      ) : submissionReviewTask && isTaskAwaitingGrading(snapshot?.status) ? (
        <SubmissionRecognitionReview
          task={submissionReviewTask}
          locale={locale}
          busy={busy}
          blocked={Boolean(requiredReview)}
          onContinue={() => void confirmSubmissionReview()}
        />
      ) : teacherReviewTask ? (
        <TeacherMaterialConfirmation
          task={teacherReviewTask}
          locale={locale}
          confirmed={materialsConfirmed}
          reviewDescription={requiredReview?.description}
        />
      ) : null}
    </div>
  );
}

function SubmissionRecognitionReview({ task, locale, busy, blocked, onContinue }: { task: Task; locale: Locale; busy: boolean; blocked: boolean; onContinue: () => void }) {
  const students = Object.values(task.student_data);
  return (
    <section className="mt-5 rounded-[10px] border border-primary/25 bg-primary/[0.035] p-4 sm:p-5" aria-labelledby="demo-submission-review-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "文字识别", "Text recognition")}</p>
          <h3 id="demo-submission-review-title" className="mt-1 text-lg font-bold">{tx(locale, "本次识别的学生作答", "Recognized submissions")}</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
            {tx(locale, "检查学生信息和作答，处理未解决的问题后开始批改。", "Check student details and answers, and resolve outstanding issues before starting grading.")}
          </p>
        </div>
        <Link className="shrink-0 text-xs font-semibold text-primary hover:underline" to={`/tasks/${task.task_id}/submissions`}>
          {tx(locale, "在作答校对页查看", "Open submission review")}
        </Link>
      </div>

      <div className="mt-4 grid items-start gap-3 md:grid-cols-2">
        {students.map((student) => (
          <article key={student.stu_id} className="rounded-lg border bg-card px-3 py-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-bold">{student.stu_name || student.stu_id}</p>
                <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{student.stu_id} · {student.source_filename || tx(locale, "作答文件", "Submission file")}</p>
              </div>
              <span className={cn("shrink-0 rounded-full px-2 py-1 text-[10px] font-semibold", student.identity_status === "needs_review" ? "bg-warning/10 text-warning" : "bg-accent/10 text-accent")}>
                {student.identity_status === "needs_review" ? tx(locale, "身份待核对", "Identity check") : tx(locale, `${student.stu_ans.length} 题已识别`, `${student.stu_ans.length} answers`)}
              </span>
            </div>
            <div className="mt-3 grid gap-2 border-t pt-3">
              {[...student.stu_ans].sort(compareStudentAnswers).map((answer) => (
                <div key={answer.q_id} className="rounded-md bg-muted/45 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-bold text-primary">{studentAnswerLabel(answer)}</span>
                    {answerReviewNextBlocked(answer) ? <span className="text-[10px] font-medium text-warning">{answer.flag.map(flag => formatSubmissionFlag(flag, locale)).join(" · ")}</span> : null}
                  </div>
                  <MarkdownMath className="mt-1 max-h-28 overflow-auto text-[11px] leading-5 text-foreground">{answer.content}</MarkdownMath>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {!blocked ? <Button onClick={onContinue} disabled={busy} className="h-10 px-4">
          {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
          {tx(locale, "开始批改", "Start grading")}
        </Button> : null}
        <span className="text-xs text-muted-foreground">{tx(locale, "需要修改时，可进入作答校对页。", "Open submission review if you need to make corrections.")}</span>
      </div>
    </section>
  );
}

function DemoResultInsights({ result, taskId, locale }: { result: TaskResultResponse; taskId: string; locale: Locale }) {
  const model = buildResultsModel(undefined, result);
  const validPercents = model.students.map((student) => student.percent).filter((value): value is number => value !== null && Number.isFinite(value));
  const passCount = validPercents.filter((value) => value >= 60).length;
  const passRate = validPercents.length ? Math.round((passCount / validPercents.length) * 100) : 0;
  const metricCards = [
    { value: String(model.students.length), label: tx(locale, "学生", "Students") },
    { value: formatPercent(model.classAveragePercent), label: tx(locale, "班级平均得分率", "Class average") },
    { value: String(model.reviewCount), label: tx(locale, "需教师关注题次", "Review signals") },
    { value: `${passRate}%`, label: tx(locale, "及格率", "Pass rate") },
  ];
  return (
    <section className="frontier-live-results mt-5 overflow-hidden rounded-[12px] border border-primary/20 bg-card p-4 sm:p-6" aria-labelledby="demo-result-insights-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "批改结果 · 学情分析", "Grading results · learning analysis")}</p>
          <h3 id="demo-result-insights-title" className="mt-1 text-xl font-bold">{tx(locale, "从真实分数看到班级表现", "See class performance from live scores")}</h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
            {tx(locale, "统计基于当前保存的批改结果。", "Statistics use the currently saved grading results.")}
          </p>
        </div>
        <div className="flex flex-wrap gap-3 text-xs font-semibold text-primary">
          <Link className="hover:underline" to={`/tasks/${taskId}/results/visualizations`}>{tx(locale, "打开完整可视化", "Open full visual analysis")}</Link>
          <Link className="hover:underline" to={`/tasks/${taskId}/review`}>{tx(locale, "复核真实结果", "Review live results")}</Link>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {metricCards.map((card, index) => (
          <div key={card.label} className="frontier-live-reveal rounded-[10px] border bg-card px-4 py-4 shadow-sm" style={{ animationDelay: `${index * 80}ms` }}>
            <strong className="text-2xl text-primary">{card.value}</strong>
            <span className="mt-1 block text-[11px] font-medium text-muted-foreground">{card.label}</span>
          </div>
        ))}
      </div>

      <LiveResultCarousel model={model} locale={locale} />

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-[10px] bg-muted/60 px-4 py-3 text-xs text-foreground">
        <span>{tx(locale, `有 ${model.reviewCount} 个题次需要关注，可查看复核详情。`, `${model.reviewCount} responses need attention. See the review details.`)}</span>
        <span className="font-semibold text-primary">{tx(locale, `班级平均 ${formatScore(model.classAverageScore)} 分`, `Class mean ${formatScore(model.classAverageScore)} points`)}</span>
      </div>
    </section>
  );
}

function LiveResultCarousel({ model, locale }: { model: ReturnType<typeof buildResultsModel>; locale: Locale }) {
  const [activeSlide, setActiveSlide] = useState(0);
  const [manuallyPaused, setManuallyPaused] = useState(false);
  const [pointerPaused, setPointerPaused] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const slideLabels = [
    tx(locale, "学生走势", "Student path"),
    tx(locale, "分数分布", "Score distribution"),
    tx(locale, "题目散点", "Question scatter"),
    tx(locale, "复核信号", "Review signals"),
  ];

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    if (manuallyPaused || pointerPaused || reducedMotion) return;
    const timer = window.setInterval(() => setActiveSlide((current) => (current + 1) % slideLabels.length), 3_000);
    return () => window.clearInterval(timer);
  }, [manuallyPaused, pointerPaused, reducedMotion, slideLabels.length]);

  return (
    <section
      className="frontier-live-reveal mt-4 overflow-hidden rounded-[12px] border border-[#cfd9ef] bg-card shadow-sm"
      style={{ animationDelay: "320ms" }}
      aria-label={tx(locale, "结果图表轮播", "Result chart carousel")}
      onMouseEnter={() => setPointerPaused(true)}
      onMouseLeave={() => setPointerPaused(false)}
      onFocusCapture={() => setPointerPaused(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPointerPaused(false);
      }}
    >
      <div className="flex flex-col gap-3 border-b bg-muted/35 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">{tx(locale, "结果图表", "Result charts")}</p>
          <h4 className="mt-1 inline-flex items-center gap-2 text-sm font-bold"><BarChart3 className="h-4 w-4 text-primary" />{slideLabels[activeSlide]}</h4>
        </div>
        <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label={tx(locale, "选择分析图", "Choose analysis chart")}>
          {slideLabels.map((label, index) => (
            <button
              key={label}
              type="button"
              role="tab"
              aria-selected={activeSlide === index}
              aria-controls="frontier-live-chart-panel"
              onClick={() => setActiveSlide(index)}
              className={cn(
                "inline-flex h-8 min-w-8 items-center justify-center rounded-full border px-2 text-[10px] font-bold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                activeSlide === index ? "border-primary bg-primary text-primary-foreground" : "border-[#d8dfed] bg-card text-muted-foreground hover:border-primary/35 hover:text-primary",
              )}
              title={label}
            >
              {String(index + 1).padStart(2, "0")}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setManuallyPaused((current) => !current)}
            className="inline-flex h-8 items-center gap-1.5 rounded-full border border-[#d8dfed] bg-card px-3 text-[10px] font-semibold text-muted-foreground transition hover:border-primary/35 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            aria-label={manuallyPaused ? tx(locale, "继续自动播放", "Resume autoplay") : tx(locale, "暂停自动播放", "Pause autoplay")}
          >
            {manuallyPaused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
            {manuallyPaused ? tx(locale, "播放", "Play") : tx(locale, "暂停", "Pause")}
          </button>
        </div>
      </div>

      <div id="frontier-live-chart-panel" role="tabpanel" className="min-h-[330px] p-4 sm:p-6">
        <div key={activeSlide} className="frontier-live-carousel-panel">
          {activeSlide === 0 ? <StudentPathChart model={model} locale={locale} /> : null}
          {activeSlide === 1 ? <ScoreDistributionChart model={model} locale={locale} /> : null}
          {activeSlide === 2 ? <QuestionScatterChart model={model} locale={locale} /> : null}
          {activeSlide === 3 ? <ReviewSignalChart model={model} locale={locale} /> : null}
        </div>
      </div>
    </section>
  );
}

function StudentPathChart({ model, locale }: { model: ReturnType<typeof buildResultsModel>; locale: Locale }) {
  const points = model.students.map((student, index) => ({
    student,
    x: model.students.length <= 1 ? 360 : 68 + index * (584 / (model.students.length - 1)),
    y: 226 - ((student.percent ?? 0) * 1.75),
  }));
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(210px,0.6fr)] lg:items-center">
      <div className="rounded-[10px] border bg-card p-3">
        <svg viewBox="0 0 720 270" className="h-[240px] w-full" role="img" aria-label={tx(locale, "学生总分率折线图", "Student total score line chart")}>
          <defs><linearGradient id="frontier-score-line" x1="0" x2="1"><stop stopColor="#5B7CFA" /><stop offset="0.5" stopColor="#B8A5FF" /><stop offset="1" stopColor="#5FD6D1" /></linearGradient></defs>
          {[25, 50, 75, 100].map((tick) => <g key={tick}><line x1="58" x2="672" y1={226 - tick * 1.75} y2={226 - tick * 1.75} stroke="#dce3f3" strokeDasharray="4 7" /><text x="12" y={230 - tick * 1.75} fill="#7d879b" fontSize="11">{tick}%</text></g>)}
          {points.length > 1 ? <polyline points={points.map((point) => `${point.x},${point.y}`).join(" ")} fill="none" stroke="url(#frontier-score-line)" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" /> : null}
          {points.map((point, index) => <g key={point.student.id}><circle cx={point.x} cy={point.y} r="11" fill={["#5B7CFA", "#5FD6D1", "#FFB69E", "#B8A5FF"][index % 4]} stroke="white" strokeWidth="5" /><text x={point.x} y="254" textAnchor="middle" fill="#596277" fontSize="11">{shortStudentName(point.student.name)}</text></g>)}
        </svg>
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "逐位学生", "Student by student")}</p>
        <h5 className="mt-2 text-xl font-bold">{tx(locale, "总分率一眼可比", "Compare total score rates")}</h5>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{tx(locale, "比较各位学生的总分率。", "Compare students’ total score percentages.")}</p>
        <div className="mt-4 grid grid-cols-2 gap-2">
          {model.students.map((student) => <div key={student.id} className="rounded-lg border bg-card px-3 py-2"><span className="block truncate text-[10px] text-muted-foreground">{student.name}</span><strong className="text-primary">{formatPercent(student.percent)}</strong></div>)}
        </div>
      </div>
    </div>
  );
}

function ScoreDistributionChart({ model, locale }: { model: ReturnType<typeof buildResultsModel>; locale: Locale }) {
  const buckets = [
    { label: "<60", min: 0, max: 59, color: "#FF9F8D" },
    { label: "60–69", min: 60, max: 69, color: "#F6D978" },
    { label: "70–79", min: 70, max: 79, color: "#78D8D1" },
    { label: "80–89", min: 80, max: 89, color: "#7D98F5" },
    { label: "90–100", min: 90, max: 100, color: "#B8A5FF" },
  ].map((bucket) => ({ ...bucket, count: model.students.filter((student) => (student.percent ?? -1) >= bucket.min && (student.percent ?? -1) <= bucket.max).length }));
  const maxCount = Math.max(1, ...buckets.map((bucket) => bucket.count));
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(210px,0.6fr)] lg:items-center">
      <div className="flex h-[270px] items-end justify-around gap-3 rounded-[10px] border bg-card px-5 pb-5 pt-8">
        {buckets.map((bucket) => <div key={bucket.label} className="flex h-full min-w-0 flex-1 flex-col items-center justify-end"><span className="mb-2 text-sm font-bold" style={{ color: bucket.color }}>{bucket.count}</span><span className="frontier-live-column w-full max-w-20 rounded-t-[14px]" style={{ height: `${Math.max(8, (bucket.count / maxCount) * 165)}px`, background: `linear-gradient(180deg, ${bucket.color}, ${bucket.color}aa)` }} /><span className="mt-3 text-[10px] font-semibold text-muted-foreground">{bucket.label}%</span></div>)}
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "分布直方图", "Score histogram")}</p>
        <h5 className="mt-2 text-xl font-bold">{tx(locale, "班级成绩落在哪些区间", "Where the class scores fall")}</h5>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{tx(locale, "查看不同得分区间的学生人数。", "View the number of students in each score range.")}</p>
      </div>
    </div>
  );
}

function QuestionScatterChart({ model, locale }: { model: ReturnType<typeof buildResultsModel>; locale: Locale }) {
  const points = model.questions.map((question, index) => ({ question, x: 92 + index * (model.questions.length <= 1 ? 0 : 520 / (model.questions.length - 1)), y: 226 - ((question.avgPercent ?? 0) * 1.75) }));
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(210px,0.6fr)] lg:items-center">
      <div className="rounded-[10px] border bg-card p-3">
        <svg viewBox="0 0 720 270" className="h-[240px] w-full" role="img" aria-label={tx(locale, "逐题平均得分率散点图", "Question average scatter plot")}>
          {[25, 50, 75, 100].map((tick) => <g key={tick}><line x1="58" x2="672" y1={226 - tick * 1.75} y2={226 - tick * 1.75} stroke="#e4ddf4" /><text x="12" y={230 - tick * 1.75} fill="#827894" fontSize="11">{tick}%</text></g>)}
          <line x1="58" x2="672" y1={121} y2={121} stroke="#FF9F8D" strokeDasharray="8 7" strokeWidth="2" />
          {points.map((point, index) => <g key={point.question.id}><circle cx={point.x} cy={point.y} r={16 + Math.min(8, point.question.reviewCount * 3)} fill={["#7D98F5", "#78D8D1", "#FF9F8D", "#B8A5FF"][index % 4]} fillOpacity="0.9" stroke="white" strokeWidth="5" /><text x={point.x} y="254" textAnchor="middle" fill="#61596f" fontSize="11">{point.question.label}</text></g>)}
        </svg>
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "逐题散点", "Question scatter")}</p>
        <h5 className="mt-2 text-xl font-bold">{tx(locale, "定位难题与复核密度", "Locate hard questions and review density")}</h5>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{tx(locale, "纵轴是本次逐题平均得分率；点越大，需复核的作答越多。", "The vertical position is this run's question average; larger dots indicate more responses needing review.")}</p>
      </div>
    </div>
  );
}

function ReviewSignalChart({ model, locale }: { model: ReturnType<typeof buildResultsModel>; locale: Locale }) {
  const totalUnits = Math.max(0, model.students.length * model.questions.length);
  const clearUnits = Math.max(0, totalUnits - model.reviewCount);
  const reviewPercent = totalUnits ? Math.round((model.reviewCount / totalUnits) * 100) : 0;
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(260px,0.8fr)_minmax(0,1.2fr)] lg:items-center">
      <div className="flex min-h-[270px] items-center justify-center rounded-[10px] border bg-card p-5">
        <div className="relative h-48 w-48 rounded-full shadow-inner" style={{ background: `conic-gradient(#FFB69E 0 ${reviewPercent}%, #A7E3C1 ${reviewPercent}% 100%)` }}>
          <div className="absolute inset-8 flex flex-col items-center justify-center rounded-full bg-card shadow-sm"><strong className="text-3xl text-primary">{reviewPercent}%</strong><span className="mt-1 text-[10px] font-semibold text-muted-foreground">{tx(locale, "需复核", "review")}</span></div>
        </div>
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "教师复核信号", "Teacher review signals")}</p>
        <h5 className="mt-2 text-xl font-bold">{tx(locale, "把注意力放在需要判断的地方", "Focus attention where judgment is needed")}</h5>
        <p className="mt-2 max-w-lg text-xs leading-5 text-muted-foreground">{tx(locale, "查看需要关注的作答占比。", "View the proportion of answers needing attention.")}</p>
        <div className="mt-5 grid grid-cols-3 gap-2">
          {[{ value: totalUnits, label: tx(locale, "题次", "Responses") }, { value: model.reviewCount, label: tx(locale, "需复核", "Review") }, { value: clearUnits, label: tx(locale, "无复核标记", "No flag") }].map((item) => <div key={item.label} className="rounded-lg border bg-card px-3 py-3"><strong className="text-xl text-primary">{item.value}</strong><span className="mt-1 block text-[10px] text-muted-foreground">{item.label}</span></div>)}
        </div>
      </div>
    </div>
  );
}

function shortStudentName(name: string) {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length <= 1) return name.slice(0, 10);
  return `${words[0]} ${words.at(-1)?.slice(0, 1) ?? ""}.`;
}

function TeacherMaterialConfirmation({
  task,
  locale,
  confirmed,
  reviewDescription,
}: {
  task: Task;
  locale: Locale;
  confirmed: boolean;
  reviewDescription?: string;
}) {
  let aligned = Object.values(task.problem_data);
  try { aligned = alignDemoProblems(aligned); } catch { /* Keep malformed results available for manual review. */ }
  return (
    <section className="mt-5 rounded-[10px] border border-primary/25 bg-primary/[0.035] p-4 sm:p-5" aria-labelledby="demo-teacher-material-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "题目资料", "Question materials")}</p>
          <h3 id="demo-teacher-material-title" className="mt-1 text-lg font-bold">{tx(locale, "本次生成的题目资料", "Generated question materials")}</h3>
          <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">
            {reviewDescription ?? tx(locale, "题干、标答与评分依据已实时生成，可以继续识别作答。", "Questions, reference answers, and rubrics were generated live. You can continue to submissions.")}
          </p>
        </div>
        <Link className="shrink-0 text-xs font-semibold text-primary hover:underline" to={`/tasks/${task.task_id}/questions`}>
          {tx(locale, "在题目审核页查看", "Open question review")}
        </Link>
      </div>

      <div className="mt-4 grid gap-3">
        {aligned.map((problem, index) => (
          <details key={problem.q_id} className="rounded-lg border bg-card px-3 py-2.5" open={index === 0}>
            <summary className="cursor-pointer text-sm font-semibold">
              {problemLabel(problem)} · {problem.type} · {tx(locale, `${problem.max_score} 分`, `${problem.max_score} points`)}
            </summary>
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{tx(locale, "题干", "Question")}</p>
                <MarkdownMath className="mt-1 text-xs leading-5 text-foreground">{problem.stem}</MarkdownMath>
              </div>
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{tx(locale, "本次生成的评分依据与标答", "Rubric and answer generated in this run")}</p>
                <MarkdownMath className="mt-1 text-xs leading-5 text-foreground">{problem.criterion}</MarkdownMath>
                <MarkdownMath className="mt-2 border-t pt-2 text-xs leading-5 text-muted-foreground">{problem.reference_answer ?? ""}</MarkdownMath>
                {problem.solution_code ? <div className="mt-2 overflow-auto"><SyntaxHighlightedCode code={problem.solution_code} languageHint={`${problem.type}\n${problem.stem}`} locale={locale} /></div> : null}
                {problem.test_cases?.length ? <p className="mt-2 text-[11px] font-semibold text-accent">{tx(locale, `${problem.test_cases.length} 个代码测试样例已生成`, `${problem.test_cases.length} programming tests generated`)}</p> : null}
              </div>
            </div>
          </details>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {confirmed ? (
          <span className="inline-flex h-10 items-center gap-2 rounded-md border border-accent/25 bg-accent/5 px-4 text-xs font-semibold text-accent">
            <Check className="h-4 w-4" />
            {tx(locale, "本次生成资料已由教师确认", "This run's generated materials were teacher-confirmed")}
          </span>
        ) : null}
        <span className="text-xs text-muted-foreground">{tx(locale, "资料会随当前任务持续保留；仍可在题目审核页查看，最终评分继续由教师复核。", "These materials remain attached to the current task and stay available in question review; final scores remain subject to teacher review.")}</span>
      </div>
    </section>
  );
}

function WorkflowStep({ step, index, locale }: { step: RunStep; index: number; locale: Locale }) {
  const Icon = step.state === "complete" ? Check : step.state === "error" ? TriangleAlert : step.state === "active" ? LoaderCircle : CircleDot;
  return (
    <li className={cn(
      "grid h-full grid-cols-[38px_minmax(0,1fr)] items-center gap-3 rounded-[9px] border px-3 py-3.5 transition",
      step.state === "active" && "border-primary/35 bg-primary/5",
      step.state === "complete" && "border-accent/25 bg-accent/5",
      step.state === "error" && "border-danger/30 bg-danger/5",
    )}>
      <span className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-full border bg-card text-xs font-bold",
        step.state === "active" && "border-primary text-primary",
        step.state === "complete" && "border-accent text-accent",
        step.state === "error" && "border-danger text-danger",
      )}>
        {step.state === "waiting" ? index + 1 : <Icon className={cn("h-4 w-4", step.state === "active" && "animate-spin")} />}
      </span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-bold text-foreground">{step.label}</p>
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{step.detail}</p>
      </div>
    </li>
  );
}

function BoundaryBadge({ icon, label, tone = "neutral", inverse = false }: { icon: React.ReactNode; label: string; tone?: "neutral" | "blue"; inverse?: boolean }) {
  return <span className={cn(
    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-semibold",
    inverse
      ? tone === "blue" ? "border-sky-300/25 bg-sky-300/10 text-sky-100" : "border-white/15 bg-white/[0.06] text-slate-200"
      : tone === "blue" ? "border-primary/25 bg-primary/5 text-primary" : "bg-card text-muted-foreground",
  )}>{icon}{label}</span>;
}

function StatusPill({ status }: { status: TaskStatus }) {
  const { t } = useI18n();
  const done = ["graded", "finalized"].includes(status);
  const label = t(TASK_STAGE_KEYS[status]);
  return <span className={cn("inline-flex min-w-max shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-4 py-1.5 text-xs font-bold", done ? "bg-accent/10 text-accent" : status === "error" ? "bg-danger/10 text-danger" : "bg-primary/10 text-primary")}><span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current" />{label}</span>;
}

function TaskLinks({ taskId, status, locale }: { taskId: string; status?: TaskStatus; locale: Locale }) {
  const finished = status && ["graded", "finalized"].includes(status);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs font-semibold text-primary">
      <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}`}>{tx(locale, "打开任务", "Open task")} <ArrowRight className="h-3.5 w-3.5" /></Link>
      {finished ? <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}/review`}>{tx(locale, "复核真实结果", "Review real results")} <ArrowRight className="h-3.5 w-3.5" /></Link> : null}
      {finished ? <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}/results/visualizations`}>{tx(locale, "查看可视化", "View visual analysis")} <ArrowRight className="h-3.5 w-3.5" /></Link> : null}
    </div>
  );
}

async function demoRecognitionProvider(taskId: string) {
  const setup = await getGradingSetup(taskId);
  const enabled = setup.available_experts.filter((provider) => provider.enabled);
  const preferred = setup.grading_setup?.primary_provider_id ?? setup.suggested_setup?.primary_provider_id;
  const selected = enabled.find((provider) => provider.provider_id === preferred) ?? enabled[0];
  if (!selected) throw new Error("No enabled provider is available for this demo session.");
  return selected.provider_id;
}

function generatedMaterialsConfirmed(problems: ProblemInfo[]) {
  return problems.length > 0 && problems.every((problem) => (
    problem.review_status === "confirmed"
  ));
}

function demoQuestionReviewBlockers(task: Task, locale: Locale): ReviewBlocker[] {
  const problems = Object.values(task.problem_data);
  const blockers = questionReviewBlockers(problems, task.task_id, locale);
  // Preserve the fixed fixture's Q1–Q4 guard, but offer its existing editor
  // instead of retrying a successfully completed recognition job.
  try { alignDemoProblems(problems); } catch {
    blockers.push({ label: tx(locale, "请核对题目数量和 Q1–Q4 题号。", "Check the question count and Q1–Q4 numbering."), href: `/tasks/${task.task_id}/questions` });
  }
  return blockers;
}

export function alignDemoProblems(problems: ProblemInfo[]) {
  if (problems.length !== EXPECTED_DEMO_QUESTION_COUNT) {
    throw new Error(`Question preparation returned ${problems.length} items; expected ${EXPECTED_DEMO_QUESTION_COUNT}. Open the task to review the real result before continuing.`);
  }
  const byNumber = new Map<number, ProblemInfo>();
  for (const problem of problems) {
    const recognizedNumber = problemNumber(problem.number) ?? problemNumber(problem.q_id);
    if (recognizedNumber === null || recognizedNumber < 1 || recognizedNumber > EXPECTED_DEMO_QUESTION_COUNT || byNumber.has(recognizedNumber)) {
      throw new Error("Question preparation did not preserve unique Q1–Q4 numbering. Open the task to inspect the real result; no generated material has been replaced.");
    }
    byNumber.set(recognizedNumber, problem);
  }
  return Array.from({ length: EXPECTED_DEMO_QUESTION_COUNT }, (_, index) => byNumber.get(index + 1)!);
}

function problemNumber(value: string) {
  const match = value.match(/\d+/);
  return match ? Number.parseInt(match[0], 10) : null;
}

function studentAnswerLabel(answer: Task["student_data"][string]["stu_ans"][number]) {
  const number = problemNumber(answer.number) ?? problemNumber(answer.q_id);
  return number === null ? answer.number || answer.q_id : `Q${number}`;
}

function compareStudentAnswers(
  left: Task["student_data"][string]["stu_ans"][number],
  right: Task["student_data"][string]["stu_ans"][number],
) {
  const leftNumber = problemNumber(left.number) ?? problemNumber(left.q_id) ?? Number.MAX_SAFE_INTEGER;
  const rightNumber = problemNumber(right.number) ?? problemNumber(right.q_id) ?? Number.MAX_SAFE_INTEGER;
  return leftNumber - rightNumber || left.q_id.localeCompare(right.q_id);
}

function progressPercent(snapshot: TaskStateSnapshot) {
  // Persisted completion wins over the reporter's last intermediate counters.
  if (snapshot.status === "problems_ready" || isTaskAwaitingGrading(snapshot.status)
    || snapshot.status === "graded" || snapshot.status === "finalized") return 100;
  const progress = snapshot.progress;
  if (!progress) return 0;
  if (progress.total_steps && progress.completed_steps !== null && progress.completed_steps !== undefined) {
    return Math.max(4, Math.min(100, Math.round((progress.completed_steps / progress.total_steps) * 100)));
  }
  const total = progress.total_students * progress.total_questions;
  return total > 0 ? Math.max(4, Math.min(100, Math.round((progress.completed_units / total) * 100))) : progress.phase === "done" ? 100 : 8;
}

function latestProgressMessage(snapshot: TaskStateSnapshot, locale: Locale) {
  const progress = snapshot.progress;
  if (isTaskPaused(snapshot.status)) return tx(locale, "本次处理已停止", "This run has stopped");
  if (snapshot.status === "graded" || snapshot.status === "finalized") return gradingProgressText(locale, "completed");
  if (progress?.activity_latest || snapshot.status === "grading") {
    return gradingActivityText(progress?.activity_latest, progress?.model_progress ?? [], locale === "zh-CN");
  }
  return recognitionEventText(progress?.messages?.at(-1), locale === "zh-CN");
}

function stepsFromSnapshot(snapshot: TaskStateSnapshot, locale: Locale): RunStep[] {
  const failedJobId = snapshot.last_failed_job_id ?? snapshot.progress?.job_id;
  const failedJobRank = failedJobId
    ? [snapshot.extract_job_id, snapshot.parse_job_id, snapshot.grading_job_id].indexOf(failedJobId) + 1
    : 0;
  const rank: Record<TaskStatus, number> = {
    problems_paused: 1,
    submissions_paused: 2,
    grading_paused: 3,
    draft: 0,
    extracting_problems: 1,
    problems_ready: 2,
    parsing_submissions: 2,
    submissions_ready: 3,
    grading_setup: 3,
    grading: 3,
    graded: 4,
    finalized: 4,
    error: failedJobRank || (snapshot.student_count > 0 ? 3 : snapshot.problem_count > 0 ? 2 : 1),
  };
  const completedRank = rank[snapshot.status];
  return initialSteps(locale).map((step, index) => {
    const state: RunStepState = snapshot.status === "error" && index === Math.max(0, completedRank)
      ? "error"
      : index < completedRank
        ? "complete"
        : index === completedRank && ["extracting_problems", "parsing_submissions", "grading"].includes(snapshot.status)
          ? "active"
          : "waiting";
    const completeDetail = step.id === "task"
      ? tx(locale, "任务已创建", "Task created")
      : step.id === "questions"
        ? tx(locale, `已识别 ${snapshot.problem_count} 题；人工确认状态请查看审核页面`, `${snapshot.problem_count} questions recognized; see question review for confirmation status`)
        : step.id === "submissions"
          ? tx(locale, `已识别 ${snapshot.student_count} 份作答`, `${snapshot.student_count} submissions recognized`)
          : tx(locale, `已处理 ${snapshot.student_count * snapshot.problem_count} 题次`, `${snapshot.student_count * snapshot.problem_count} responses processed`);
    return {
      ...step,
      state,
      detail: state === "complete" ? completeDetail : step.detail,
      jobId: step.id === "questions" ? snapshot.extract_job_id : step.id === "submissions" ? snapshot.parse_job_id : step.id === "grading" ? snapshot.grading_job_id : null,
    };
  });
}

function tx(locale: Locale, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

function liveDemoWorkflowFailureMessage(locale: Locale, detail: string) {
  if (detail === "provider_unreachable") {
    return tx(
      locale,
      "模型服务暂时不可用，请稍后重试。已完成的内容可在任务中查看。",
      "The model service is unavailable. Retry later or open the task to view completed work.",
    );
  }
  if (detail === "shared_pool_daily_limit_reached") {
    return tx(
      locale,
      "已超出每日体验限额，请在额度重置后再试。",
      "The daily demo limit has been reached. Please try again after the allowance resets.",
    );
  }
  return tx(locale, `本次运行已停止：${detail}`, `This run stopped: ${detail}`);
}

function errorMessage(error: unknown) {
  const normalized = normalizeAPIError(error);
  return { message: normalized.message || "Unknown live-demo error.", code: getAPIErrorCode(normalized) };
}

function createIdempotencyKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? `frontier-${crypto.randomUUID()}`
    : `frontier-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}


function safeTaskId(value: string | null) {
  return value && /^[A-Za-z0-9_-]{1,96}$/.test(value) && isFrontierTaskPath(`/tasks/${value}`) ? value : null;
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

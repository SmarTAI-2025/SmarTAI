import {
  Activity,
  ArrowRight,
  Check,
  CircleDot,
  Clock3,
  ExternalLink,
  FileText,
  FlaskConical,
  LoaderCircle,
  Play,
  ScanText,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { preflightProblemSource, startQuestionPreparation } from "@/api/problemSources";
import {
  createTask,
  getTask,
  getTaskState,
  parseSubmissions,
  startGrading,
  updateProblem,
} from "@/api/tasks";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { MarkdownMath } from "@/components/ui/MarkdownMath";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import type { GradingSetup, ProblemInfo, Task, TaskStateSnapshot, TaskStatus } from "@/types";

const QUESTION_FIXTURE = "/frontier-demo/live/question_source.pdf";
const SUBMISSION_FIXTURE = "/frontier-demo/live/submissions_raw.zip";
const HANDWRITTEN_FIXTURE = "/frontier-demo/live/DEMO-002_handwritten_raw.png";
const FIXTURE_MANIFEST = "/frontier-demo/manifest.json";
const POLL_INTERVAL_MS = 1_500;
const WORKFLOW_TIMEOUT_MS = 8 * 60 * 1_000;
const EXPECTED_DEMO_QUESTION_COUNT = 4;
const DEMO_SCORE_POLICY = "Q1: 5 points; Q2: 8 points; Q3: 7 points; Q4: 10 points.";

type RunStepId = "task" | "questions" | "submissions" | "grading";
type RunStepState = "waiting" | "active" | "complete" | "error";

interface RunStep {
  id: RunStepId;
  label: string;
  detail: string;
  state: RunStepState;
  jobId?: string | null;
}

function initialSteps(locale: Locale): RunStep[] {
  return [
    { id: "task", label: tx(locale, "创建专属 Demo 任务", "Create a dedicated demo task"), detail: tx(locale, "真实 POST /tasks 请求", "Real POST /tasks request"), state: "waiting" },
    { id: "questions", label: tx(locale, "准备并确认题目", "Prepare and confirm questions"), detail: tx(locale, "真实识别 + 标答与评分资料生成", "Real extraction + answer and rubric generation"), state: "waiting" },
    { id: "submissions", label: tx(locale, "识别混合作答", "Recognize mixed submissions"), detail: tx(locale, "真实 PDF / 图片 OCR 流程", "Real PDF/image OCR pipeline"), state: "waiting" },
    { id: "grading", label: tx(locale, "运行 AI 批改", "Run AI grading"), detail: tx(locale, "真实单模型批改任务", "Real single-provider grading run"), state: "waiting" },
  ];
}

export function FrontierLiveDemoPage() {
  const { locale } = useI18n();
  const navigate = useNavigate();
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
  const [materialsConfirmed, setMaterialsConfirmed] = useState(false);
  const activeStepRef = useRef<RunStepId | null>(null);
  const runLockRef = useRef(false);

  useEffect(() => {
    if (!taskIdFromUrl || busy || runLockRef.current) return;
    const restoredTaskId = taskIdFromUrl;
    let cancelled = false;
    async function restoreCurrentTask() {
      try {
        const state = await getTaskState(restoredTaskId);
        if (cancelled) return;
        setTaskId(restoredTaskId);
        setSnapshot(state);
        setSteps((current) => current.some((step) => step.state === "error") ? current : stepsFromSnapshot(state, locale));
        if (state.problem_count > 0) {
          const task = await getTask(restoredTaskId);
          if (cancelled) return;
          const prepared = alignDemoProblems(Object.values(task.problem_data));
          if (!preparedMaterialsComplete(prepared)) {
            throw new Error(tx(locale, "此任务的题目准备资料不完整；请打开任务检查真实结果。", "This task's prepared question materials are incomplete. Open the task to inspect the real result."));
          }
          const confirmed = generatedMaterialsConfirmed(prepared);
          setTeacherReviewTask(task);
          setMaterialsConfirmed(confirmed);
          if (state.status === "problems_ready" && !confirmed) {
            return;
          }
        }
        if (["draft", "extracting_problems", "problems_ready", "parsing_submissions", "submissions_ready", "grading"].includes(state.status)) {
          runLockRef.current = true;
          setBusy(true);
          setError(null);
          setStartedAt(Date.now());
          await continueWorkflow(restoredTaskId, state.status);
        }
      } catch (caught) {
        if (!cancelled) {
          failActiveStep();
          setError(errorMessage(caught));
        }
      } finally {
        if (!cancelled) {
          runLockRef.current = false;
          setBusy(false);
        }
      }
    }
    void restoreCurrentTask();
    return () => { cancelled = true; };
  }, [locale, taskIdFromUrl]);

  useEffect(() => {
    if (busy) return;
    setSteps(snapshot ? stepsFromSnapshot(snapshot, locale) : initialSteps(locale));
  }, [busy, locale, snapshot]);

  const elapsed = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1_000)) : null;
  async function startFreshRun() {
    if (runLockRef.current) return;
    runLockRef.current = true;
    setBusy(true);
    setError(null);
    setSnapshot(null);
    setProviderLabel(null);
    setTeacherReviewTask(null);
    setMaterialsConfirmed(false);
    setStartedAt(Date.now());
    setSteps(initialSteps(locale));
    activeStepRef.current = null;
    try {
      markStep("task", "active", tx(locale, "正在创建真实教师任务…", "Creating a real teacher-owned task…"));
      const task = await createTask({
        name: `SmarTAI Live Demo · ${new Date().toISOString().slice(0, 16).replace("T", " ")} UTC`,
        idempotencyKey: createIdempotencyKey(),
      });
      setTaskId(task.task_id);
      markStep("task", "complete", tx(locale, `任务 ${shortId(task.task_id)} 已创建`, `Task ${shortId(task.task_id)} created`));
      navigate(`/frontier/live?taskId=${encodeURIComponent(task.task_id)}`, { replace: true });
      await continueWorkflow(task.task_id, task.status);
    } catch (caught) {
      failActiveStep();
      setError(errorMessage(caught));
    } finally {
      runLockRef.current = false;
      setBusy(false);
    }
  }

  async function confirmTeacherMaterials() {
    if (!taskId || !teacherReviewTask || runLockRef.current) return;
    runLockRef.current = true;
    setBusy(true);
    setError(null);
    try {
      markStep("questions", "active", tx(locale, "正在确认本次真实生成的题目资料…", "Confirming the materials generated in this live run…"));
      await confirmPreparedQuestions(taskId, teacherReviewTask);
      setMaterialsConfirmed(true);
      await continueWorkflow(taskId, "problems_ready", true);
    } catch (caught) {
      failActiveStep();
      setError(errorMessage(caught));
    } finally {
      runLockRef.current = false;
      setBusy(false);
    }
  }

  async function continueWorkflow(currentTaskId: string, startingStatus: TaskStatus, teacherMaterialsConfirmed = false) {
    let status = startingStatus;

    if (status === "draft") {
      markStep("questions", "active", tx(locale, "正在上传原始题目并运行完整题目准备…", "Uploading the raw source and running full question preparation…"));
      const questionSource = await fixtureFile(QUESTION_FIXTURE, "question_source.pdf", "application/pdf");
      const preflight = await preflightProblemSource({
        taskId: currentTaskId,
        mode: "upload",
        role: "problem",
        file: questionSource,
        structureMode: "organized",
        extractionHint: tx(locale, "按 Q1–Q4 保留题号与数学符号。", "Preserve the Q1–Q4 numbering and mathematical notation."),
        saveToLibrary: false,
      });
      const expectedWorkflowRevision = preflight.workflow_revision ?? preflight.base_workflow_revision;
      if (expectedWorkflowRevision === undefined) {
        throw new Error(tx(locale, "题目准备预检未返回工作流版本。", "Question-preparation preflight did not return a workflow revision."));
      }
      const response = await startQuestionPreparation({
        taskId: currentTaskId,
        sourceTokens: [preflight.source_token],
        expectedWorkflowRevision,
        scorePolicy: { mode: "per_question", perQuestionText: DEMO_SCORE_POLICY },
      });
      markStep("questions", "active", tx(locale, "正在识别题目并生成标答、评分依据与代码题材料…", "Recognizing questions and generating answers, rubrics, and programming materials…"), response.job_id);
      const state = await waitForStatus(currentTaskId, ["problems_ready"]);
      status = state.status;
    } else if (status === "extracting_problems") {
      markStep("questions", "active", tx(locale, "正在等待已有题目识别任务…", "Waiting for the existing question-recognition job…"));
      status = (await waitForStatus(currentTaskId, ["problems_ready"])).status;
    }

    if (status === "problems_ready") {
      const recognizedTask = await getTask(currentTaskId);
      const aligned = alignDemoProblems(Object.values(recognizedTask.problem_data));
      if (!preparedMaterialsComplete(aligned)) {
        throw new Error(tx(locale, "本次题目准备未生成完整的标答、评分依据或代码题材料；请打开任务检查真实结果。", "This preparation run did not generate a complete answer, rubric, or programming package. Open the task to inspect the real result."));
      }
      setTeacherReviewTask(recognizedTask);
      const alreadyConfirmed = generatedMaterialsConfirmed(aligned);
      setMaterialsConfirmed(teacherMaterialsConfirmed || alreadyConfirmed);
      if (!teacherMaterialsConfirmed && !alreadyConfirmed) {
        const recognizedState = await getTaskState(currentTaskId);
        setSnapshot(recognizedState);
        markStep("questions", "active", tx(locale, "题目、标答与评分资料已真实生成；等待教师确认。", "Questions, answers, and rubrics were generated live and await teacher confirmation."), recognizedState.extract_job_id);
        return;
      }
      const afterQuestions = await getTaskState(currentTaskId);
      setSnapshot(afterQuestions);
      markStep("questions", "complete", tx(locale, `已识别 ${afterQuestions.problem_count} 题并确认教师评分标准`, `${afterQuestions.problem_count} questions recognized and teacher rubric confirmed`), afterQuestions.extract_job_id);

      markStep("submissions", "active", tx(locale, "正在上传原始排版、混排与手写作答…", "Uploading raw typeset, mixed, and handwritten submissions…"));
      const archive = await fixtureFile(SUBMISSION_FIXTURE, "submissions_raw.zip", "application/zip");
      const response = await parseSubmissions({ taskId: currentTaskId, file: archive, identityMode: "filename" });
      markStep("submissions", "active", tx(locale, "作答识别与 OCR 正在运行…", "Submission recognition and OCR are running…"), response.job_id);
      status = (await waitForStatus(currentTaskId, ["submissions_ready"])).status;
    } else if (status === "parsing_submissions") {
      markStep("questions", "complete", tx(locale, "题目已准备", "Questions already prepared"));
      markStep("submissions", "active", tx(locale, "正在等待已有作答识别任务…", "Waiting for the existing submission-recognition job…"));
      status = (await waitForStatus(currentTaskId, ["submissions_ready"])).status;
    }

    if (status === "submissions_ready") {
      const recognized = await getTaskState(currentTaskId);
      setSnapshot(recognized);
      markStep("submissions", "complete", tx(locale, `已识别 ${recognized.student_count} 份合成作答`, `${recognized.student_count} synthetic submissions recognized`), recognized.parse_job_id);
      markStep("grading", "active", tx(locale, "正在选择一个已启用模型并保存批改设置…", "Selecting one enabled provider and saving the grading setup…"));
      const setupResponse = await getGradingSetup(currentTaskId);
      const enabled = setupResponse.available_experts.filter((item) => item.enabled);
      const preferredId = setupResponse.grading_setup?.primary_provider_id ?? setupResponse.suggested_setup?.primary_provider_id;
      const selected = enabled.find((item) => item.provider_id === preferredId) ?? enabled[0];
      if (!selected) throw new Error(tx(locale, "当前 Demo 会话没有可用的批改模型。", "No enabled grading provider is available for this demo session."));
      setProviderLabel(selected.display_name || `${selected.provider_type} · ${selected.model}`);
      const base = setupResponse.grading_setup ?? setupResponse.suggested_setup;
      if (!base) throw new Error(tx(locale, "后端未返回有效的批改设置。", "The backend did not return a valid grading setup."));
      const singleSetup: GradingSetup = {
        ...base,
        selected_provider_ids: [selected.provider_id],
        primary_provider_id: selected.provider_id,
        aggregation_method: "single",
        multi_sample_n: 1,
        knowledge_scope: "none",
        feedback_language: locale === "zh-CN" ? "zh" : "en",
      };
      await saveGradingSetup({
        taskId: currentTaskId,
        expectedWorkflowRevision: setupResponse.workflow_revision,
        gradingSetup: singleSetup,
      });
      const response = await startGrading(currentTaskId, { multiSampleN: 1 });
      markStep("grading", "active", tx(locale, "真实批改任务正在运行…", "The real grading run is in progress…"), response.job_id);
      status = (await waitForStatus(currentTaskId, ["graded", "review_confirmed", "finalized"])).status;
    } else if (status === "grading") {
      markStep("questions", "complete", tx(locale, "题目已准备", "Questions already prepared"));
      markStep("submissions", "complete", tx(locale, "作答已识别", "Submissions already recognized"));
      markStep("grading", "active", tx(locale, "正在等待已有批改任务…", "Waiting for the existing grading job…"));
      status = (await waitForStatus(currentTaskId, ["graded", "review_confirmed", "finalized"])).status;
    }

    if (["graded", "review_confirmed", "finalized"].includes(status)) {
      const completed = await getTaskState(currentTaskId);
      setSnapshot(completed);
      markStep("grading", "complete", tx(locale, `已处理 ${completed.student_count * completed.problem_count} 个作答单元`, `${completed.student_count * completed.problem_count} answer units processed`), completed.grading_job_id);
    }
  }

  async function waitForStatus(currentTaskId: string, targets: TaskStatus[]) {
    const deadline = Date.now() + WORKFLOW_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const state = await getTaskState(currentTaskId);
      setSnapshot(state);
      if (targets.includes(state.status)) return state;
      if (state.status === "error") {
        const detail = state.progress?.error_detail || state.error || "workflow_error";
        throw new Error(tx(locale, `后端已停止本次运行：${detail}`, `The backend stopped this run: ${detail}`));
      }
      await delay(POLL_INTERVAL_MS);
    }
    throw new Error(tx(locale, "真实流程已超过八分钟；任务已保留，可继续检查或恢复。", "The live run exceeded eight minutes. The task is preserved so you can inspect or resume it."));
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
      <div className="flex flex-col gap-5 border-b pb-7 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <Link to="/frontier" className="text-xs font-semibold uppercase tracking-[0.16em] text-primary hover:underline">SmarTAI · {tx(locale, "产品介绍", "product overview")}</Link>
          <h1 className="mt-3 text-[34px] font-bold leading-tight tracking-[-0.035em] text-foreground sm:text-[42px]">{tx(locale, "真实批改运行", "Live grading run")}</h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground sm:text-base">
            {tx(locale, "合成作业会经过真实的 SmarTAI API、原文识别、OCR 与批改流程。本页不会向真实结果注入任何预计算分数。", "Synthetic coursework goes through the real SmarTAI API, source recognition, OCR, and grading pipeline. Nothing on this page injects precomputed scores into the live result.")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <BoundaryBadge icon={<FlaskConical />} label={tx(locale, "合成输入", "Synthetic inputs")} />
          <BoundaryBadge icon={<Activity />} label={tx(locale, "真实 API", "Real API")} tone="blue" />
          <BoundaryBadge icon={<ScanText />} label={tx(locale, "真实 OCR", "Real OCR")} tone="blue" />
          <BoundaryBadge icon={<Sparkles />} label={tx(locale, "真实批改", "Real grading")} tone="blue" />
        </div>
      </div>

      <InlineNotice tone="info" title={tx(locale, "免密码 Demo 会话，不暴露模型密钥", "Passwordless demo session, no exposed model key")} className="mt-6">
        {tx(locale, "从展示页进入时，后端会签发短时 Demo 会话。Gemini API Key 仅保存在后端，不会写入本页面、文件包或 URL。", "Entering from the showcase creates a short-lived demo session. The Gemini API key remains server-side and is never embedded in this page, fixture archive, or URL.")}
      </InlineNotice>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(330px,0.9fr)]">
        <section className="flex h-full flex-col rounded-[12px] border bg-card p-5 shadow-sm sm:p-7">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{tx(locale, "可观察的真实流程", "Observable live workflow")}</p>
              <h2 className="mt-2 text-2xl font-bold tracking-[-0.025em]">{tx(locale, "运行完整样例任务", "Run the complete sample task")}</h2>
              <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{tx(locale, "每次运行都会使用模型额度、创建独立任务并保留真实 job ID，所有中间结果均可进入产品页面检查。", "Each run uses model quota, creates its own task, preserves the real job IDs, and leaves every intermediate result open for inspection.")}</p>
            </div>
            {snapshot ? <StatusPill status={snapshot.status} locale={locale} /> : null}
          </div>

          <ol className="mt-7 grid flex-1 grid-rows-4 gap-3">
            {steps.map((step, index) => <WorkflowStep key={step.id} step={step} index={index} locale={locale} />)}
          </ol>

          {error ? (
            <InlineNotice
              tone="danger"
              title={tx(locale, "真实后端返回错误", "The live backend returned an error")}
              className="mt-5"
              action={<Link className="text-xs font-semibold underline" to="/frontier#walkthrough">{tx(locale, "查看预计算导览", "View precomputed walkthrough")}</Link>}
            >
              {error.code ? <span className="mr-2 rounded bg-danger/10 px-1.5 py-0.5 font-mono text-xs">{error.code}</span> : null}
              {error.message} {tx(locale, "未用静态分数替换真实结果。", "No static score has been substituted.")}
            </InlineNotice>
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <Button className="h-11 px-5" onClick={startFreshRun} disabled={busy}>
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {busy ? tx(locale, "正在运行真实流程…", "Running real workflow…") : taskId ? tx(locale, "开始新的真实运行", "Start a fresh live run") : tx(locale, "开始真实 OCR + 批改", "Start real OCR + grading")}
            </Button>
            {taskId ? <TaskLinks taskId={taskId} status={snapshot?.status} locale={locale} /> : null}
          </div>

          <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
            {taskId ? <span className="font-mono">task {shortId(taskId)}</span> : <span>{tx(locale, "尚未创建任务", "No task created yet")}</span>}
            {providerLabel ? <span>{tx(locale, "模型", "provider")} {providerLabel}</span> : null}
            {elapsed !== null ? <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{tx(locale, `已观察 ${elapsed} 秒`, `${elapsed}s observed`)}</span> : null}
          </div>

          {snapshot?.progress ? (
            <div className="mt-5 rounded-[9px] border bg-muted/30 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-semibold">
                <span className="inline-flex items-center gap-2 text-foreground"><Activity className="h-4 w-4 text-primary" />{tx(locale, "后端进度", "Backend progress")}</span>
                <span className="font-mono text-muted-foreground">{snapshot.progress.current_step || snapshot.progress.phase}</span>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
                <span className="block h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${progressPercent(snapshot)}%` }} />
              </div>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">{latestProgressMessage(snapshot) || tx(locale, "正在等待下一个后端事件…", "Waiting for the next backend event…")}</p>
            </div>
          ) : null}
        </section>

        <aside className="grid content-start gap-4">
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><FileText className="h-4 w-4 text-primary" />{tx(locale, "原始题目样例", "Raw question fixture")}</div>
              <a href={QUESTION_FIXTURE} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">{tx(locale, "打开", "Open")} <ExternalLink className="h-3.5 w-3.5" /></a>
            </div>
            <object data={QUESTION_FIXTURE} type="application/pdf" title="Synthetic raw question source" className="h-[310px] w-full bg-white">
              <a href={QUESTION_FIXTURE}>{tx(locale, "打开题目 PDF", "Open the question PDF")}</a>
            </object>
          </section>
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><ScanText className="h-4 w-4 text-primary" />{tx(locale, "原始手写样例", "Raw handwritten fixture")}</div>
              <span className="rounded-full bg-warning/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-warning">{tx(locale, "OCR 输入", "OCR input")}</span>
            </div>
            <div className="flex h-[280px] items-start justify-center overflow-auto bg-slate-100 p-3">
              <img src={HANDWRITTEN_FIXTURE} alt="Raw synthetic handwritten student submission without grades or teacher annotations" className="max-w-full bg-white shadow-md" />
            </div>
            <p className="border-t px-4 py-3 text-xs leading-5 text-muted-foreground">{tx(locale, "这就是实际上传至 API 的浏览器端合成样例副本；它不代表当前后端已持久化原始文件字节。", "This is the same browser-side synthetic fixture uploaded to the API. It is not a claim that the current backend persists original bytes.")}</p>
          </section>
          <InlineNotice tone="warning" title={tx(locale, "仍由教师控制的部分", "What remains human-controlled")}>
            {tx(locale, "题目准备流程真实生成标答与评分依据，教师确认后才进入批改。批改完成后，教师仍需在复核工作台检查证据并作出最终发布决定。", "The live preparation flow generates the reference answers and rubrics, and grading starts only after teacher confirmation. The educator still reviews evidence and makes the final release decision.")}
          </InlineNotice>
        </aside>
      </div>

      {teacherReviewTask ? (
        <TeacherMaterialConfirmation
          task={teacherReviewTask}
          locale={locale}
          busy={busy}
          confirmed={materialsConfirmed}
          onConfirm={() => void confirmTeacherMaterials()}
        />
      ) : null}
    </div>
  );
}

function TeacherMaterialConfirmation({
  task,
  locale,
  busy,
  confirmed,
  onConfirm,
}: {
  task: Task;
  locale: Locale;
  busy: boolean;
  confirmed: boolean;
  onConfirm: () => void;
}) {
  const aligned = alignDemoProblems(Object.values(task.problem_data));
  return (
    <section className="mt-5 rounded-[10px] border border-primary/25 bg-primary/[0.035] p-4 sm:p-5" aria-labelledby="demo-teacher-material-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">{confirmed ? tx(locale, "真实生成 · 已确认", "Live generation · confirmed") : tx(locale, "真实生成 · 教师确认", "Live generation · teacher confirmation")}</p>
          <h3 id="demo-teacher-material-title" className="mt-1 text-lg font-bold">{tx(locale, "复核本次生成的题目资料", "Review this run's generated materials")}</h3>
          <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">
            {tx(locale, "题干、标答、评分依据与代码题材料均来自本次真实题目准备流程；这里只做教师确认，不注入预置答案或评分标准。各题总分来自合成 Demo 中明确的教师设置。", "The stems, reference answers, rubrics, and programming materials all come from this live preparation run. This step only records teacher confirmation; it injects no pre-authored answers or rubrics. Question totals come from the synthetic Demo's explicit teacher score settings.")}
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
              Q{problem.number} · {problem.type} · {tx(locale, `${problem.max_score} 分`, `${problem.max_score} points`)}
            </summary>
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{tx(locale, "本次真实识别题干", "Live recognized question")}</p>
                <MarkdownMath className="mt-1 text-xs leading-5 text-foreground">{problem.stem}</MarkdownMath>
              </div>
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{tx(locale, "本次生成的评分依据与标答", "Rubric and answer generated in this run")}</p>
                <MarkdownMath className="mt-1 text-xs leading-5 text-foreground">{problem.criterion}</MarkdownMath>
                <MarkdownMath className="mt-2 border-t pt-2 text-xs leading-5 text-muted-foreground">{problem.reference_answer ?? ""}</MarkdownMath>
                {problem.solution_code ? <pre className="mt-2 overflow-auto rounded-md bg-slate-950 p-2 text-[11px] text-slate-100"><code>{problem.solution_code}</code></pre> : null}
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
        ) : (
          <Button onClick={onConfirm} disabled={busy} className="h-10 px-4">
            {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {tx(locale, "确认生成资料并继续真实 OCR", "Confirm generated materials and continue live OCR")}
          </Button>
        )}
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
          {step.jobId ? <span className="font-mono text-[10px] text-muted-foreground">{tx(locale, "任务", "job")} {shortId(step.jobId)}</span> : null}
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{step.detail}</p>
      </div>
    </li>
  );
}

function BoundaryBadge({ icon, label, tone = "neutral" }: { icon: React.ReactNode; label: string; tone?: "neutral" | "blue" }) {
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-semibold", tone === "blue" ? "border-primary/25 bg-primary/5 text-primary" : "bg-card text-muted-foreground")}>{icon}{label}</span>;
}

function StatusPill({ status, locale }: { status: TaskStatus; locale: Locale }) {
  const done = ["graded", "review_confirmed", "finalized"].includes(status);
  const zhStatus: Partial<Record<TaskStatus, string>> = {
    draft: "草稿",
    extracting_problems: "识别题目中",
    problems_ready: "题目已准备",
    parsing_submissions: "识别作答中",
    submissions_ready: "作答已准备",
    grading: "批改中",
    graded: "批改完成",
    review_confirmed: "复核完成",
    generating_analysis: "生成分析中",
    finalized: "已完成",
    error: "错误",
  };
  const label = locale === "zh-CN" ? zhStatus[status] ?? status : status.replaceAll("_", " ");
  return <span className={cn("inline-flex min-w-max shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-4 py-1.5 text-xs font-bold", done ? "bg-accent/10 text-accent" : status === "error" ? "bg-danger/10 text-danger" : "bg-primary/10 text-primary")}><span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current" />{label}</span>;
}

function TaskLinks({ taskId, status, locale }: { taskId: string; status?: TaskStatus; locale: Locale }) {
  const finished = status && ["graded", "review_confirmed", "finalized"].includes(status);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs font-semibold text-primary">
      <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}`}>{tx(locale, "打开任务", "Open task")} <ArrowRight className="h-3.5 w-3.5" /></Link>
      {finished ? <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}/review`}>{tx(locale, "复核真实结果", "Review real results")} <ArrowRight className="h-3.5 w-3.5" /></Link> : null}
    </div>
  );
}

async function confirmPreparedQuestions(taskId: string, task?: Task) {
  const currentTask = task ?? await getTask(taskId);
  const aligned = alignDemoProblems(Object.values(currentTask.problem_data));
  if (!preparedMaterialsComplete(aligned)) {
    throw new Error("The live question-preparation result is incomplete; no generated material was replaced with a fixture fallback.");
  }
  for (const problem of aligned) {
    await updateProblem(taskId, problem.q_id, {
      // Persist only the materials returned by the real preparation run. The
      // Demo never substitutes fixture answers, rubrics, code, or tests.
      stem: problem.stem,
      criterion: problem.criterion,
      max_score: problem.max_score,
      reference_answer: problem.reference_answer ?? null,
      solution_code: problem.solution_code ?? null,
      review_status: "confirmed",
      test_cases: problem.test_cases ?? null,
    });
  }
}

function generatedMaterialsConfirmed(problems: ProblemInfo[]) {
  return problems.every((problem) => (
    problem.review_status === "confirmed"
  ));
}

function preparedMaterialsComplete(problems: ProblemInfo[]) {
  return problems.every((problem) => (
    Boolean(problem.stem.trim())
    && Boolean(problem.criterion.trim())
    && Boolean(problem.reference_answer?.trim())
    && (problemNumber(problem.number) !== 4 || (
      Boolean(problem.solution_code?.trim())
      && Boolean(problem.test_cases?.length)
    ))
  ));
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

async function fixtureFile(url: string, filename: string, type: string) {
  const [response, manifest] = await Promise.all([
    fetch(url, { cache: "no-store" }),
    fixtureManifest(),
  ]);
  if (!response.ok) throw new Error(`Could not load the synthetic fixture ${filename} (${response.status}).`);
  const blob = await response.blob();
  const relativePath = url.replace(/^\/frontier-demo\//, "");
  const entry = manifest.assets.find((asset) => asset.path === relativePath);
  if (!entry) throw new Error(`The fixture manifest does not contain ${filename}.`);
  const digest = await sha256Hex(await blob.arrayBuffer());
  if (digest !== entry.sha256) throw new Error(`Fixture integrity check failed for ${filename}.`);
  return new File([blob], filename, { type, lastModified: 0 });
}

let manifestPromise: Promise<FixtureManifest> | null = null;

function fixtureManifest() {
  manifestPromise ??= fetch(FIXTURE_MANIFEST, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Could not load the fixture manifest (${response.status}).`);
      const body = await response.json() as Partial<FixtureManifest>;
      if (!Array.isArray(body.assets)) throw new Error("The fixture manifest is invalid.");
      return { assets: body.assets };
    })
    .catch((error) => {
      manifestPromise = null;
      throw error;
    });
  return manifestPromise;
}

async function sha256Hex(value: ArrayBuffer) {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

interface FixtureManifest {
  assets: Array<{ path: string; bytes: number; sha256: string }>;
}

function progressPercent(snapshot: TaskStateSnapshot) {
  const progress = snapshot.progress;
  if (!progress) return 0;
  if (progress.total_steps && progress.completed_steps !== null && progress.completed_steps !== undefined) {
    return Math.max(4, Math.min(100, Math.round((progress.completed_steps / progress.total_steps) * 100)));
  }
  const total = progress.total_students * progress.total_questions;
  return total > 0 ? Math.max(4, Math.min(100, Math.round((progress.completed_units / total) * 100))) : progress.phase === "done" ? 100 : 8;
}

function latestProgressMessage(snapshot: TaskStateSnapshot) {
  return snapshot.progress?.messages?.at(-1)?.message ?? null;
}

function stepsFromSnapshot(snapshot: TaskStateSnapshot, locale: Locale): RunStep[] {
  const rank: Record<TaskStatus, number> = {
    draft: 0,
    extracting_problems: 1,
    problems_ready: 2,
    parsing_submissions: 2,
    submissions_ready: 3,
    grading: 3,
    graded: 4,
    review_confirmed: 4,
    generating_analysis: 4,
    finalized: 4,
    error: 0,
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
    const questionsConfirmed = snapshot.status !== "problems_ready";
    const completeDetail = step.id === "task"
      ? tx(locale, `任务 ${shortId(snapshot.task_id)} 已创建`, `Task ${shortId(snapshot.task_id)} created`)
      : step.id === "questions"
        ? questionsConfirmed
          ? tx(locale, `已识别 ${snapshot.problem_count} 题并确认教师评分标准`, `${snapshot.problem_count} questions recognized and teacher rubric confirmed`)
          : tx(locale, `已识别 ${snapshot.problem_count} 题；继续前将校验评分标准确认状态`, `${snapshot.problem_count} questions recognized; rubric confirmation will be verified before continuing`)
        : step.id === "submissions"
          ? tx(locale, `已识别 ${snapshot.student_count} 份合成作答`, `${snapshot.student_count} synthetic submissions recognized`)
          : tx(locale, `已处理 ${snapshot.student_count * snapshot.problem_count} 个作答单元`, `${snapshot.student_count * snapshot.problem_count} answer units processed`);
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

function errorMessage(error: unknown) {
  const normalized = normalizeAPIError(error);
  return { message: normalized.message || "Unknown live-demo error.", code: getAPIErrorCode(normalized) };
}

function createIdempotencyKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? `frontier-${crypto.randomUUID()}`
    : `frontier-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function shortId(value: string) {
  return value.length > 12 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value;
}

function safeTaskId(value: string | null) {
  return value && /^[A-Za-z0-9_-]{1,96}$/.test(value) ? value : null;
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

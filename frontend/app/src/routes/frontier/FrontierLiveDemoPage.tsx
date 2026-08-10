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
  RotateCcw,
  ScanText,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import {
  createTask,
  extractProblems,
  getTask,
  getTaskState,
  parseSubmissions,
  startGrading,
  updateProblem,
} from "@/api/tasks";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { demoQuestions, type DemoQuestion, type DemoQuestionId } from "@/data/frontierDemo";
import { cn } from "@/lib/cn";
import type { GradingSetup, ProblemInfo, TaskStateSnapshot, TaskStatus, TestCase } from "@/types";

const QUESTION_FIXTURE = "/frontier-demo/live/question_source.pdf";
const SUBMISSION_FIXTURE = "/frontier-demo/live/submissions_raw.zip";
const HANDWRITTEN_FIXTURE = "/frontier-demo/live/DEMO-002_handwritten_raw.png";
const FIXTURE_MANIFEST = "/frontier-demo/manifest.json";
const POLL_INTERVAL_MS = 1_500;
const WORKFLOW_TIMEOUT_MS = 8 * 60 * 1_000;

type RunStepId = "task" | "questions" | "submissions" | "grading";
type RunStepState = "waiting" | "active" | "complete" | "error";

interface RunStep {
  id: RunStepId;
  label: string;
  detail: string;
  state: RunStepState;
  jobId?: string | null;
}

const initialSteps: RunStep[] = [
  { id: "task", label: "Create a dedicated demo task", detail: "Real POST /tasks request", state: "waiting" },
  { id: "questions", label: "Recognize and confirm questions", detail: "Real source extraction + teacher rubric", state: "waiting" },
  { id: "submissions", label: "Recognize mixed submissions", detail: "Real PDF/image OCR pipeline", state: "waiting" },
  { id: "grading", label: "Run AI grading", detail: "Real single-provider grading run", state: "waiting" },
];

export function FrontierLiveDemoPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const taskIdFromUrl = safeTaskId(searchParams.get("taskId"));
  const [taskId, setTaskId] = useState<string | null>(taskIdFromUrl);
  const [snapshot, setSnapshot] = useState<TaskStateSnapshot | null>(null);
  const [steps, setSteps] = useState<RunStep[]>(initialSteps);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; code?: string | null } | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [providerLabel, setProviderLabel] = useState<string | null>(null);
  const activeStepRef = useRef<RunStepId | null>(null);
  const runLockRef = useRef(false);

  useEffect(() => {
    if (!taskIdFromUrl || busy) return;
    let cancelled = false;
    getTaskState(taskIdFromUrl)
      .then((state) => {
        if (cancelled) return;
        setTaskId(taskIdFromUrl);
        setSnapshot(state);
        setSteps((current) => current.some((step) => step.state === "error") ? current : stepsFromSnapshot(state));
      })
      .catch((caught) => {
        if (!cancelled) setError(errorMessage(caught));
      });
    return () => { cancelled = true; };
  }, [busy, taskIdFromUrl]);

  const elapsed = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1_000)) : null;
  const finished = snapshot?.status === "graded" || snapshot?.status === "review_confirmed" || snapshot?.status === "finalized";
  const canResume = Boolean(taskId && snapshot && !finished && snapshot.status !== "error");

  async function startFreshRun() {
    if (runLockRef.current) return;
    runLockRef.current = true;
    setBusy(true);
    setError(null);
    setSnapshot(null);
    setProviderLabel(null);
    setStartedAt(Date.now());
    setSteps(initialSteps);
    activeStepRef.current = null;
    try {
      markStep("task", "active", "Creating a real teacher-owned task…");
      const task = await createTask({
        name: `AWS Frontier Live Demo · ${new Date().toISOString().slice(0, 16).replace("T", " ")} UTC`,
        idempotencyKey: createIdempotencyKey(),
      });
      setTaskId(task.task_id);
      markStep("task", "complete", `Task ${shortId(task.task_id)} created`);
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

  async function resumeRun() {
    if (!taskId || !snapshot || runLockRef.current) return;
    runLockRef.current = true;
    setBusy(true);
    setError(null);
    setStartedAt((value) => value ?? Date.now());
    try {
      await continueWorkflow(taskId, snapshot.status);
    } catch (caught) {
      failActiveStep();
      setError(errorMessage(caught));
    } finally {
      runLockRef.current = false;
      setBusy(false);
    }
  }

  async function continueWorkflow(currentTaskId: string, startingStatus: TaskStatus) {
    let status = startingStatus;

    if (status === "draft") {
      markStep("questions", "active", "Uploading the raw question source to the recognition API…");
      const questionSource = await fixtureFile(QUESTION_FIXTURE, "question_source.pdf", "application/pdf");
      const response = await extractProblems(currentTaskId, questionSource);
      markStep("questions", "active", "Question recognition is running…", response.job_id);
      const state = await waitForStatus(currentTaskId, ["problems_ready"]);
      status = state.status;
    } else if (status === "extracting_problems") {
      markStep("questions", "active", "Waiting for the existing question-recognition job…");
      status = (await waitForStatus(currentTaskId, ["problems_ready"])).status;
    }

    if (status === "problems_ready") {
      markStep("questions", "active", "Applying the synthetic teacher rubric to recognized questions…");
      await confirmDemoQuestions(currentTaskId);
      const afterQuestions = await getTaskState(currentTaskId);
      setSnapshot(afterQuestions);
      markStep("questions", "complete", `${afterQuestions.problem_count} questions recognized and teacher rubric confirmed`, afterQuestions.extract_job_id);

      markStep("submissions", "active", "Uploading raw typeset, mixed, and handwritten submissions…");
      const archive = await fixtureFile(SUBMISSION_FIXTURE, "submissions_raw.zip", "application/zip");
      const response = await parseSubmissions({ taskId: currentTaskId, file: archive, identityMode: "filename" });
      markStep("submissions", "active", "Submission recognition and OCR are running…", response.job_id);
      status = (await waitForStatus(currentTaskId, ["submissions_ready"])).status;
    } else if (status === "parsing_submissions") {
      markStep("questions", "complete", "Questions already prepared");
      markStep("submissions", "active", "Waiting for the existing submission-recognition job…");
      status = (await waitForStatus(currentTaskId, ["submissions_ready"])).status;
    }

    if (status === "submissions_ready") {
      const recognized = await getTaskState(currentTaskId);
      setSnapshot(recognized);
      markStep("submissions", "complete", `${recognized.student_count} synthetic submissions recognized`, recognized.parse_job_id);
      markStep("grading", "active", "Selecting one enabled provider and saving the grading setup…");
      const setupResponse = await getGradingSetup(currentTaskId);
      const enabled = setupResponse.available_experts.filter((item) => item.enabled);
      const preferredId = setupResponse.grading_setup?.primary_provider_id ?? setupResponse.suggested_setup?.primary_provider_id;
      const selected = enabled.find((item) => item.provider_id === preferredId) ?? enabled[0];
      if (!selected) throw new Error("No enabled grading provider is available for this account.");
      setProviderLabel(selected.display_name || `${selected.provider_type} · ${selected.model}`);
      const base = setupResponse.grading_setup ?? setupResponse.suggested_setup;
      if (!base) throw new Error("The backend did not return a valid grading setup.");
      const singleSetup: GradingSetup = {
        ...base,
        selected_provider_ids: [selected.provider_id],
        primary_provider_id: selected.provider_id,
        aggregation_method: "single",
        multi_sample_n: 1,
        knowledge_scope: "none",
      };
      await saveGradingSetup({
        taskId: currentTaskId,
        expectedWorkflowRevision: setupResponse.workflow_revision,
        gradingSetup: singleSetup,
      });
      const response = await startGrading(currentTaskId, { multiSampleN: 1 });
      markStep("grading", "active", "The real grading run is in progress…", response.job_id);
      status = (await waitForStatus(currentTaskId, ["graded", "review_confirmed", "finalized"])).status;
    } else if (status === "grading") {
      markStep("questions", "complete", "Questions already prepared");
      markStep("submissions", "complete", "Submissions already recognized");
      markStep("grading", "active", "Waiting for the existing grading job…");
      status = (await waitForStatus(currentTaskId, ["graded", "review_confirmed", "finalized"])).status;
    }

    if (["graded", "review_confirmed", "finalized"].includes(status)) {
      const completed = await getTaskState(currentTaskId);
      setSnapshot(completed);
      markStep("grading", "complete", `${completed.student_count * completed.problem_count} answer units processed`, completed.grading_job_id);
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
        throw new Error(`The backend stopped this run: ${detail}`);
      }
      await delay(POLL_INTERVAL_MS);
    }
    throw new Error("The live run exceeded eight minutes. The task is preserved so you can inspect or resume it.");
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
          <Link to="/frontier" className="text-xs font-semibold uppercase tracking-[0.16em] text-primary hover:underline">AWS From Idea to Frontier showcase</Link>
          <h1 className="mt-3 text-[34px] font-bold leading-tight tracking-[-0.035em] text-foreground sm:text-[42px]">Live grading run</h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground sm:text-base">
            Synthetic coursework goes through the real SmarTAI API, source recognition, OCR, and grading pipeline. Nothing on this page injects precomputed scores into the live result.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <BoundaryBadge icon={<FlaskConical />} label="Synthetic inputs" />
          <BoundaryBadge icon={<Activity />} label="Real API" tone="blue" />
          <BoundaryBadge icon={<ScanText />} label="Real OCR" tone="blue" />
          <BoundaryBadge icon={<Sparkles />} label="Real grading" tone="blue" />
        </div>
      </div>

      <InlineNotice tone="info" title="Reviewer-specific access, no embedded credentials" className="mt-6">
        Sign in with the short-lived teacher account issued privately to you. Credentials are never reused across reviewers, and no username, password, model token, or API key is stored in this page, fixture archive, or URL.
      </InlineNotice>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(330px,0.9fr)]">
        <section className="rounded-[12px] border bg-card p-5 shadow-sm sm:p-7">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">Controlled live workflow</p>
              <h2 className="mt-2 text-2xl font-bold tracking-[-0.025em]">Run the complete sample task</h2>
              <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">One run uses model quota. It creates a new teacher-owned task, preserves the real job IDs, and leaves every intermediate result open for inspection.</p>
            </div>
            {snapshot ? <StatusPill status={snapshot.status} /> : null}
          </div>

          <ol className="mt-7 grid gap-3">
            {steps.map((step, index) => <WorkflowStep key={step.id} step={step} index={index} />)}
          </ol>

          {snapshot?.progress ? (
            <div className="mt-5 rounded-[9px] border bg-muted/30 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-semibold">
                <span className="inline-flex items-center gap-2 text-foreground"><Activity className="h-4 w-4 text-primary" />Backend progress</span>
                <span className="font-mono text-muted-foreground">{snapshot.progress.current_step || snapshot.progress.phase}</span>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
                <span className="block h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${progressPercent(snapshot)}%` }} />
              </div>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">{latestProgressMessage(snapshot) || "Waiting for the next backend event…"}</p>
            </div>
          ) : null}

          {error ? (
            <InlineNotice
              tone="danger"
              title="The live backend returned an error"
              className="mt-5"
              action={<Link className="text-xs font-semibold underline" to="/frontier#walkthrough">View precomputed walkthrough</Link>}
            >
              {error.code ? <span className="mr-2 rounded bg-danger/10 px-1.5 py-0.5 font-mono text-xs">{error.code}</span> : null}
              {error.message} No static score has been substituted.
            </InlineNotice>
          ) : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <Button className="h-11 px-5" onClick={startFreshRun} disabled={busy}>
              {busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {busy ? "Running real workflow…" : taskId ? "Start a fresh live run" : "Start real OCR + grading"}
            </Button>
            {canResume ? (
              <Button variant="secondary" className="h-11 px-5" onClick={resumeRun} disabled={busy}>
                <RotateCcw className="h-4 w-4" />Resume this task
              </Button>
            ) : null}
            {taskId ? <TaskLinks taskId={taskId} status={snapshot?.status} /> : null}
          </div>

          <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
            {taskId ? <span className="font-mono">task {shortId(taskId)}</span> : <span>No task created yet</span>}
            {providerLabel ? <span>provider {providerLabel}</span> : null}
            {elapsed !== null ? <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{elapsed}s observed</span> : null}
          </div>
        </section>

        <aside className="grid content-start gap-4">
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><FileText className="h-4 w-4 text-primary" />Raw question fixture</div>
              <a href={QUESTION_FIXTURE} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline">Open <ExternalLink className="h-3.5 w-3.5" /></a>
            </div>
            <object data={QUESTION_FIXTURE} type="application/pdf" title="Synthetic raw question source" className="h-[310px] w-full bg-white">
              <a href={QUESTION_FIXTURE}>Open the question PDF</a>
            </object>
          </section>
          <section className="overflow-hidden rounded-[12px] border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-bold"><ScanText className="h-4 w-4 text-primary" />Raw handwritten fixture</div>
              <span className="rounded-full bg-warning/10 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-warning">OCR input</span>
            </div>
            <div className="flex h-[280px] items-start justify-center overflow-auto bg-slate-100 p-3">
              <img src={HANDWRITTEN_FIXTURE} alt="Raw synthetic handwritten student submission without grades or teacher annotations" className="max-w-full bg-white shadow-md" />
            </div>
            <p className="border-t px-4 py-3 text-xs leading-5 text-muted-foreground">This is the same browser-side fixture uploaded to the API. It is a synthetic source copy, not a claim that the current backend persists original bytes.</p>
          </section>
          <InlineNotice tone="warning" title="What remains human-controlled">
            The sample rubric is supplied as synthetic teacher input. After grading, the existing review workspace still requires the educator to inspect evidence and make the final release decision.
          </InlineNotice>
        </aside>
      </div>
    </div>
  );
}

function WorkflowStep({ step, index }: { step: RunStep; index: number }) {
  const Icon = step.state === "complete" ? Check : step.state === "error" ? TriangleAlert : step.state === "active" ? LoaderCircle : CircleDot;
  return (
    <li className={cn(
      "grid grid-cols-[38px_minmax(0,1fr)] gap-3 rounded-[9px] border px-3 py-3.5 transition",
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
          {step.jobId ? <span className="font-mono text-[10px] text-muted-foreground">job {shortId(step.jobId)}</span> : null}
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{step.detail}</p>
      </div>
    </li>
  );
}

function BoundaryBadge({ icon, label, tone = "neutral" }: { icon: React.ReactNode; label: string; tone?: "neutral" | "blue" }) {
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-xs font-semibold", tone === "blue" ? "border-primary/25 bg-primary/5 text-primary" : "bg-card text-muted-foreground")}>{icon}{label}</span>;
}

function StatusPill({ status }: { status: TaskStatus }) {
  const done = ["graded", "review_confirmed", "finalized"].includes(status);
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-bold", done ? "bg-accent/10 text-accent" : status === "error" ? "bg-danger/10 text-danger" : "bg-primary/10 text-primary")}><span className="h-1.5 w-1.5 rounded-full bg-current" />{status.replaceAll("_", " ")}</span>;
}

function TaskLinks({ taskId, status }: { taskId: string; status?: TaskStatus }) {
  const finished = status && ["graded", "review_confirmed", "finalized"].includes(status);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs font-semibold text-primary">
      <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}`}>Open task <ArrowRight className="h-3.5 w-3.5" /></Link>
      {finished ? <Link className="inline-flex items-center gap-1 hover:underline" to={`/tasks/${taskId}/review`}>Review real results <ArrowRight className="h-3.5 w-3.5" /></Link> : null}
    </div>
  );
}

async function confirmDemoQuestions(taskId: string) {
  const task = await getTask(taskId);
  const aligned = alignDemoProblems(Object.values(task.problem_data));
  for (const { problem, fixture } of aligned) {
    await updateProblem(taskId, problem.q_id, {
      criterion: fixture.rubric.map((item, rubricIndex) => `${rubricIndex + 1}. ${item}`).join("\n"),
      max_score: fixture.maxScore,
      reference_answer: fixture.reference,
      review_status: "confirmed",
      test_cases: fixture.id === "q4" ? stableSoftmaxTests() : null,
    });
  }
}

const DEMO_QUESTION_ANCHORS: Record<DemoQuestionId, string[][]> = {
  q1: [["integral01", "int01", "01xexp", "01xe"], ["exp", "ex2"], ["x2"], ["substitution"]],
  q2: [["2kg"], ["incline"], ["020", "02"], ["acceleration"], ["speed"]],
  q3: [["kera", "kernela"], ["ata", "transpose"], ["rank"], ["invertible"]],
  q4: [["stablesoftmax"], ["emptyinput"], ["1000"], ["numpy"]],
};

export function alignDemoProblems(problems: ProblemInfo[]) {
  if (problems.length !== demoQuestions.length) {
    throw new Error(`Question recognition returned ${problems.length} items; expected ${demoQuestions.length}. Open the task to review the real extraction before continuing.`);
  }
  const remaining = new Set(problems);
  return demoQuestions.map((fixture, index) => {
    const expectedNumber = index + 1;
    const candidates = [...remaining].filter((problem) => {
      const recognizedNumber = problemNumber(problem.number);
      if (recognizedNumber !== null && recognizedNumber !== expectedNumber) return false;
      const normalized = normalizeQuestionText(problem.stem);
      return DEMO_QUESTION_ANCHORS[fixture.id].every((alternatives) =>
        alternatives.some((anchor) => normalized.includes(normalizeQuestionText(anchor))),
      );
    });
    if (candidates.length !== 1) {
      throw new Error(`Question recognition did not preserve a unique semantic match for ${fixture.label}. Open the task to inspect the real extraction; no rubric or score has been applied.`);
    }
    const problem = candidates[0];
    remaining.delete(problem);
    return { problem, fixture } satisfies { problem: ProblemInfo; fixture: DemoQuestion };
  });
}

function normalizeQuestionText(value: string) {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replaceAll("²", "2")
    .replaceAll("ᵀ", "t")
    .replace(/[^a-z0-9]+/g, "");
}

function problemNumber(value: string) {
  const match = value.match(/\d+/);
  return match ? Number.parseInt(match[0], 10) : null;
}

function stableSoftmaxTests(): TestCase[] {
  return [
    testCase("Empty input", [[]], "[]", "Return an empty list for empty input."),
    testCase("Balanced pair", [[0, 0]], "[0.5, 0.5]", "Normalize equal logits."),
    testCase("Large values", [[1000, 1000]], "[0.5, 0.5]", "Avoid exponential overflow."),
  ];
}

function testCase(title: string, args: unknown[], expected: string, description: string): TestCase {
  return {
    title,
    description,
    input: JSON.stringify(args),
    expected_output: expected,
    source: "teacher",
    sandbox_feasible: true,
    visibility: "hidden",
    purpose: "boundary",
    io_mode: "function",
    function_name: "stable_softmax",
    function_args: args,
    expected_return: expected,
  };
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

function stepsFromSnapshot(snapshot: TaskStateSnapshot): RunStep[] {
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
  return initialSteps.map((step, index) => {
    const state: RunStepState = snapshot.status === "error" && index === Math.max(0, completedRank)
      ? "error"
      : index < completedRank
        ? "complete"
        : index === completedRank && ["extracting_problems", "parsing_submissions", "grading"].includes(snapshot.status)
          ? "active"
          : "waiting";
    const questionsConfirmed = snapshot.status !== "problems_ready";
    const completeDetail = step.id === "task"
      ? `Task ${shortId(snapshot.task_id)} created`
      : step.id === "questions"
        ? questionsConfirmed
          ? `${snapshot.problem_count} questions recognized and teacher rubric confirmed`
          : `${snapshot.problem_count} questions recognized; rubric confirmation will be verified before continuing`
        : step.id === "submissions"
          ? `${snapshot.student_count} synthetic submissions recognized`
          : `${snapshot.student_count * snapshot.problem_count} answer units processed`;
    return {
      ...step,
      state,
      detail: state === "complete" ? completeDetail : step.detail,
      jobId: step.id === "questions" ? snapshot.extract_job_id : step.id === "submissions" ? snapshot.parse_job_id : step.id === "grading" ? snapshot.grading_job_id : null,
    };
  });
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

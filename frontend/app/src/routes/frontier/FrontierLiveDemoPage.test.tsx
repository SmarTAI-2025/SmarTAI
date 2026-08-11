import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { preflightProblemSource, startQuestionPreparation } from "@/api/problemSources";
import { createTask, getTask, getTaskResult, getTaskState, parseSubmissions, startGrading, updateProblem } from "@/api/tasks";
import { demoQuestions } from "@/data/frontierDemo";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { Correction, GradingSetupResponse, Task, TaskResultResponse, TaskStateSnapshot } from "@/types";
import { alignDemoProblems, FrontierLiveDemoPage } from "./FrontierLiveDemoPage";

vi.mock("@/api/tasks", () => ({
  createTask: vi.fn(),
  getTask: vi.fn(),
  getTaskResult: vi.fn(),
  getTaskState: vi.fn(),
  parseSubmissions: vi.fn(),
  startGrading: vi.fn(),
  updateProblem: vi.fn(),
}));

vi.mock("@/api/problemSources", () => ({
  preflightProblemSource: vi.fn(),
  startQuestionPreparation: vi.fn(),
}));

vi.mock("@/api/gradingSetup", () => ({
  getGradingSetup: vi.fn(),
  saveGradingSetup: vi.fn(),
}));

vi.mock("@/components/tasks/PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ title }: { title: string }) => <div role="document" aria-label={title} />,
}));

describe("FrontierLiveDemoPage", () => {
  beforeEach(() => window.localStorage.setItem("smartai_locale", "en-US"));

  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("states the honest boundary between synthetic inputs and live processing", () => {
    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(screen.getByText("Synthetic inputs")).toBeInTheDocument();
    expect(screen.getByText("Real API")).toBeInTheDocument();
    expect(screen.getByText("Real OCR")).toBeInTheDocument();
    expect(screen.getByText("Real grading")).toBeInTheDocument();
    expect(screen.getByText(/Nothing on this page injects precomputed scores/i)).toBeInTheDocument();
    expect(screen.getByText(/no exposed model key/i)).toBeInTheDocument();
  });

  it("offers an explicit real-run action", () => {
    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    expect(screen.getByRole("button", { name: /start real OCR \+ grading/i })).toBeEnabled();
  });

  it("preserves real recognized content instead of testing it against fixture keywords", () => {
    const problems = Object.values(taskWithQuestions().problem_data);
    problems[1] = { ...problems[1], stem: "Implement stable_softmax for values near 1000." };

    expect(alignDemoProblems(problems)[1].stem).toBe("Implement stable_softmax for values near 1000.");
  });

  it("aligns recognized questions by semantic identity instead of object order", () => {
    const problems = Object.values(taskWithQuestions().problem_data).reverse();

    expect(alignDemoProblems(problems).map((problem) => problem.q_id)).toEqual(["q1", "q2", "q3", "q4"]);
  });

  it("stops before rubric confirmation when recognized question numbers are duplicated", () => {
    const problems = Object.values(taskWithQuestions().problem_data);
    problems[1] = { ...problems[1], number: "Q1", q_id: "q1-copy" };

    expect(() => alignDemoProblems(problems)).toThrow(/unique Q1–Q4 numbering/i);
  });

  it("restores the same teacher-confirmation view without a resume button", async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
          <FrontierLiveDemoPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByRole("heading", { name: /review this run's generated materials/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume this task/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/teacher rubric confirmed/i)).not.toBeInTheDocument();
  });

  it("keeps generated materials visible after returning under React StrictMode", async () => {
    const user = userEvent.setup();
    const completedTask = taskWithQuestions();
    Object.values(completedTask.problem_data).forEach((problem) => {
      problem.review_status = "confirmed";
    });
    vi.mocked(getTaskState).mockResolvedValue(taskState("graded"));
    vi.mocked(getTask).mockResolvedValue(completedTask);
    vi.mocked(createTask).mockImplementation(() => new Promise(() => {}));

    render(
      <StrictMode>
        <I18nProvider>
          <MemoryRouter initialEntries={["/tasks/asg_demo123"]}>
            <Routes>
              <Route path="/tasks/:taskId" element={<Link to="/frontier/live?taskId=asg_demo123">Live Demo</Link>} />
              <Route path="/frontier/live" element={<FrontierLiveDemoPage />} />
            </Routes>
          </MemoryRouter>
        </I18nProvider>
      </StrictMode>,
    );

    await user.click(screen.getByRole("link", { name: "Live Demo" }));
    expect(await screen.findByRole("heading", { name: /review this run's generated materials/i })).toBeInTheDocument();
    expect(screen.getByText(/generated materials were teacher-confirmed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm generated materials/i })).not.toBeInTheDocument();
    expect(getTask).toHaveBeenCalledWith("asg_demo123");

    await user.click(screen.getByRole("button", { name: /start a fresh live run/i }));
    expect(screen.queryByRole("heading", { name: /review this run's generated materials/i })).not.toBeInTheDocument();
  });

  it("runs the real API workflow in order without injecting fallback scores", async () => {
    const user = userEvent.setup();
    const recognizedTask = taskWithQuestions();
    recognizedTask.student_data = recognizedStudents();
    recognizedTask.problem_data.q2.stem = "Recognized Q2 wording from the live extraction.";
    vi.mocked(createTask).mockResolvedValue(taskState("draft"));
    vi.mocked(preflightProblemSource).mockResolvedValue({
      status: "ready",
      source_token: "source-questions",
      source: { kind: "upload", filename: "question_source.pdf", size_bytes: 7, sha256: "0".repeat(64) },
      structure_mode: "organized",
      requires_confirmation: false,
      candidate_summary: { matched: [], possible_matches: [], not_found: [], semantic_match_performed: false },
      workflow_revision: 0,
    });
    vi.mocked(startQuestionPreparation).mockResolvedValue({ status: "started", job_id: "job-questions" });
    vi.mocked(getTask).mockResolvedValue(recognizedTask);
    vi.mocked(updateProblem).mockResolvedValue({ status: "ok", q_id: "q", problem: taskWithQuestions().problem_data.q1 });
    vi.mocked(parseSubmissions).mockResolvedValue({ status: "started", job_id: "job-submissions" });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockResolvedValue({ ...gradingSetup(), configured: true, status: "saved" });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "job-grading" });
    vi.mocked(getTaskResult).mockResolvedValue(gradedResult(recognizedTask));
    vi.mocked(getTaskState).mockImplementation(async () => {
      if (vi.mocked(startGrading).mock.calls.length) return taskState("graded");
      if (vi.mocked(parseSubmissions).mock.calls.length) return taskState("submissions_ready");
      return taskState("problems_ready");
    });

    vi.stubGlobal("crypto", {
      randomUUID: () => "demo-run-id",
      subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).buffer) },
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("manifest.json")) {
        return {
          ok: true,
          json: async () => ({
            assets: [
              { path: "live/question_source.pdf", bytes: 7, sha256: "0".repeat(64) },
              { path: "live/submissions_raw.zip", bytes: 7, sha256: "0".repeat(64) },
            ],
          }),
        } as Response;
      }
      const fixture = new Blob(["fixture"]);
      Object.defineProperty(fixture, "arrayBuffer", {
        value: async () => new TextEncoder().encode("fixture").buffer,
      });
      return { ok: true, status: 200, blob: async () => fixture } as Response;
    }));

    render(<I18nProvider><MemoryRouter><FrontierLiveDemoPage /></MemoryRouter></I18nProvider>);
    await user.click(screen.getByRole("button", { name: /start real OCR \+ grading/i }));

    await waitFor(() => expect(startQuestionPreparation).toHaveBeenCalledTimes(1));
    const confirmTeacherMaterials = await screen.findByRole("button", { name: /confirm generated materials and continue live OCR/i });
    expect(parseSubmissions).not.toHaveBeenCalled();
    expect(screen.getByText(/come from this live preparation run/i)).toBeInTheDocument();
    await user.click(confirmTeacherMaterials);
    await waitFor(() => expect(updateProblem).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(parseSubmissions).toHaveBeenCalledTimes(1));
    expect(saveGradingSetup).not.toHaveBeenCalled();
    expect(await screen.findByRole("heading", { name: /inspect this run's recognized submissions/i })).toBeInTheDocument();
    expect(screen.getByText("Alex Chen")).toBeVisible();
    expect(screen.getByText("Maya Lin")).toBeVisible();
    expect(screen.getByText("Jordan Rivera")).toBeVisible();
    expect(screen.getByText("Taylor Singh")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-1")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-2")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-3")).toBeVisible();
    expect(screen.getByText("Recognized answer for q1 by demo-4")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /continue to live grading/i }));
    await waitFor(() => expect(saveGradingSetup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(startGrading).toHaveBeenCalledTimes(1));
    await screen.findByText("16 answer units processed");
    expect(await screen.findByRole("heading", { name: /see class performance from live scores/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/live result chart carousel/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /compare total score rates/i })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "02" }));
    expect(screen.getByRole("heading", { name: /where the class scores fall/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pause autoplay/i })).toBeInTheDocument();
    expect(createTask).toHaveBeenCalledTimes(1);
    expect(preflightProblemSource).toHaveBeenCalledTimes(1);
    expect(startQuestionPreparation).toHaveBeenCalledTimes(1);
    expect(updateProblem).toHaveBeenCalledTimes(4);
    expect(parseSubmissions).toHaveBeenCalledTimes(1);
    expect(saveGradingSetup).toHaveBeenCalledTimes(1);
    expect(startGrading).toHaveBeenCalledTimes(1);
    expect(saveGradingSetup).toHaveBeenCalledWith(expect.objectContaining({
      gradingSetup: expect.objectContaining({ feedback_language: "en" }),
    }));
    expect(updateProblem).toHaveBeenCalledWith(
      "asg_demo123",
      "q2",
      expect.objectContaining({ stem: "Recognized Q2 wording from the live extraction." }),
    );
    expect(updateProblem).toHaveBeenCalledWith(
      "asg_demo123",
      "q4",
      expect.objectContaining({
        solution_code: expect.stringContaining("def stable_softmax"),
        criterion: "Generated rubric for q4",
        reference_answer: "Generated answer for q4",
      }),
    );
    expect(startQuestionPreparation).toHaveBeenCalledWith(expect.objectContaining({
      scorePolicy: expect.objectContaining({ mode: "per_question" }),
    }));
    expect(screen.queryByText(/static score has been substituted/i)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/provider Demo Vision/)).toBeInTheDocument());
  });
});

function taskState(status: TaskStateSnapshot["status"]): TaskStateSnapshot {
  return {
    task_id: "asg_demo123",
    name: "SmarTAI Live Demo",
    owner_id: "teacher-demo",
    status,
    workflow_revision: 1,
    problem_count: status === "draft" ? 0 : 4,
    student_count: ["submissions_ready", "grading", "graded"].includes(status) ? 4 : 0,
    kb_docs: {},
    kb_doc_count: 0,
    created_at: 1,
    updated_at: 1,
  };
}

function recognizedStudents(): Task["student_data"] {
  const names = ["Alex Chen", "Maya Lin", "Jordan Rivera", "Taylor Singh"];
  const filenames = [
    "DEMO-001_Alex-Chen_typeset.pdf",
    "DEMO-002_Maya-Lin_handwritten.png",
    "DEMO-003_Jordan-Rivera_mixed.pdf",
    "DEMO-004_Taylor-Singh_scan.png",
  ];
  return Object.fromEntries(Array.from({ length: 4 }, (_, index) => {
    const id = `demo-${index + 1}`;
    return [id, {
      stu_id: id,
      stu_name: names[index],
      source_filename: filenames[index],
      identity_status: "matched" as const,
      identity_match_method: "filename" as const,
      stu_ans: Object.values(taskWithQuestions().problem_data).map((problem) => ({
        q_id: problem.q_id,
        number: problem.number,
        type: problem.type,
        content: `Recognized answer for ${problem.q_id} by ${id}`,
        flag: [],
        review_status: "pending" as const,
      })),
    }];
  }));
}

function gradedResult(task: Task): TaskResultResponse {
  const correction = (qId: string, maxScore: number, index: number): Correction => ({
    q_id: qId,
    type: qId === "q4" ? "programming" : "calculation",
    score: Math.max(0, maxScore - index),
    provisional_score: Math.max(0, maxScore - index),
    max_score: maxScore,
    confidence: 0.82,
    comment: "Live model feedback",
    steps: [],
    expert_results: [],
    requires_human_review: index === 2,
    review_reasons: index === 2 ? ["low_confidence"] : [],
  });
  return {
    status: "completed",
    task_id: task.task_id,
    problem_data: task.problem_data,
    student_data: task.student_data,
    results: Object.values(task.student_data).map((student, studentIndex) => ({
      student_id: student.stu_id,
      student_name: student.stu_name,
      student_answers: student.stu_ans,
      corrections: Object.values(task.problem_data).map((problem, qIndex) => correction(problem.q_id, problem.max_score, (studentIndex + qIndex) % 3)),
    })),
  };
}

function taskWithQuestions(): Task {
  const base = taskState("problems_ready");
  const problem = (qId: string, number: string, stem: string) => ({
    q_id: qId,
    number,
    type: qId === "q4" ? "programming" : "calculation",
    stem,
    criterion: `Generated rubric for ${qId}`,
    max_score: qId === "q1" ? 5 : qId === "q2" ? 8 : qId === "q3" ? 7 : 10,
    reference_answer: `Generated answer for ${qId}`,
    solution_code: qId === "q4" ? "def stable_softmax(xs):\n    return []" : null,
    test_cases: qId === "q4" ? [{
      input: "[[]]",
      expected_output: "[]",
      description: "Generated empty-input test",
      source: "llm_generated" as const,
      sandbox_feasible: true,
    }] : null,
    review_status: "needs_review" as const,
  });
  return {
    ...base,
    problem_data: {
      q1: problem("q1", "1", demoQuestions[0].prompt),
      q2: problem("q2", "2", demoQuestions[1].prompt),
      q3: problem("q3", "3", demoQuestions[2].prompt),
      q4: problem("q4", "4", demoQuestions[3].prompt),
    },
    student_data: {},
  };
}

function gradingSetup(): GradingSetupResponse {
  return {
    task_id: "asg_demo123",
    task_status: "submissions_ready",
    workflow_revision: 2,
    configured: false,
    grading_setup: null,
    suggested_setup: {
      schema_version: 1,
      selected_provider_ids: ["demo:vision"],
      primary_provider_id: "demo:vision",
      aggregation_method: "single",
      multi_sample_n: 1,
      knowledge_scope: "none",
      strictness: 0.5,
      allow_partial_credit: true,
      feedback_tone: "neutral",
      feedback_length: "medium",
      feedback_language: "en",
      suggest_corrections: true,
      low_confidence_threshold: 0.6,
      teacher_notes: "",
    },
    grading_setup_fingerprint: null,
    grading_setup_updated_at: null,
    available_experts: [{
      provider_id: "demo:vision",
      provider_type: "demo",
      model: "vision",
      display_name: "Demo Vision",
      enabled: true,
      scope: "owner",
      is_shared: false,
      editable: true,
      max_concurrent: 1,
      rpm: 10,
    }],
    knowledge: { scope_options: ["none"], task_doc_count: 0, task_docs: [] },
    readiness: { ready: true, blocking_issues: [], warnings: [] },
  };
}

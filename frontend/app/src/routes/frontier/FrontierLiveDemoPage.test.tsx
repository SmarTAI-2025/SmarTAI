import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getGradingSetup, saveGradingSetup } from "@/api/gradingSetup";
import { createTask, extractProblems, getTask, getTaskState, parseSubmissions, startGrading, updateProblem } from "@/api/tasks";
import { demoQuestions } from "@/data/frontierDemo";
import type { GradingSetupResponse, Task, TaskStateSnapshot } from "@/types";
import { alignDemoProblems, FrontierLiveDemoPage } from "./FrontierLiveDemoPage";

vi.mock("@/api/tasks", () => ({
  createTask: vi.fn(),
  extractProblems: vi.fn(),
  getTask: vi.fn(),
  getTaskState: vi.fn(),
  parseSubmissions: vi.fn(),
  startGrading: vi.fn(),
  updateProblem: vi.fn(),
}));

vi.mock("@/api/gradingSetup", () => ({
  getGradingSetup: vi.fn(),
  saveGradingSetup: vi.fn(),
}));

describe("FrontierLiveDemoPage", () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("states the honest boundary between synthetic inputs and live processing", () => {
    render(<MemoryRouter><FrontierLiveDemoPage /></MemoryRouter>);
    expect(screen.getByText("Synthetic inputs")).toBeInTheDocument();
    expect(screen.getByText("Real API")).toBeInTheDocument();
    expect(screen.getByText("Real OCR")).toBeInTheDocument();
    expect(screen.getByText("Real grading")).toBeInTheDocument();
    expect(screen.getByText(/Nothing on this page injects precomputed scores/i)).toBeInTheDocument();
    expect(screen.getByText(/no embedded credentials/i)).toBeInTheDocument();
  });

  it("offers an explicit real-run action", () => {
    render(<MemoryRouter><FrontierLiveDemoPage /></MemoryRouter>);
    expect(screen.getByRole("button", { name: /start real OCR \+ grading/i })).toBeEnabled();
  });

  it("stops before rubric confirmation when four recognized questions are semantically mismatched", () => {
    const problems = Object.values(taskWithQuestions().problem_data);
    problems[1] = { ...problems[1], stem: "Implement stable_softmax for values near 1000." };

    expect(() => alignDemoProblems(problems)).toThrow(/unique semantic match for Q2/i);
  });

  it("aligns recognized questions by semantic identity instead of object order", () => {
    const problems = Object.values(taskWithQuestions().problem_data).reverse();

    expect(alignDemoProblems(problems).map(({ problem }) => problem.q_id)).toEqual(["q1", "q2", "q3", "q4"]);
  });

  it("does not claim rubric confirmation when a refreshed task is only problems-ready", async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState("problems_ready"));

    render(
      <MemoryRouter initialEntries={["/frontier/live?taskId=asg_demo123"]}>
        <FrontierLiveDemoPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText(/rubric confirmation will be verified before continuing/i)).toBeInTheDocument();
    expect(screen.queryByText(/teacher rubric confirmed/i)).not.toBeInTheDocument();
  });

  it("runs the real API workflow in order without injecting fallback scores", async () => {
    const user = userEvent.setup();
    vi.mocked(createTask).mockResolvedValue(taskState("draft"));
    vi.mocked(extractProblems).mockResolvedValue({ status: "started", job_id: "job-questions" });
    vi.mocked(getTask).mockResolvedValue(taskWithQuestions());
    vi.mocked(updateProblem).mockResolvedValue({ status: "ok", q_id: "q", problem: taskWithQuestions().problem_data.q1 });
    vi.mocked(parseSubmissions).mockResolvedValue({ status: "started", job_id: "job-submissions" });
    vi.mocked(getGradingSetup).mockResolvedValue(gradingSetup());
    vi.mocked(saveGradingSetup).mockResolvedValue({ ...gradingSetup(), configured: true, status: "saved" });
    vi.mocked(startGrading).mockResolvedValue({ status: "started", job_id: "job-grading" });
    vi.mocked(getTaskState)
      .mockResolvedValueOnce(taskState("problems_ready"))
      .mockResolvedValueOnce(taskState("problems_ready"))
      .mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValueOnce(taskState("submissions_ready"))
      .mockResolvedValueOnce(taskState("graded"))
      .mockResolvedValueOnce(taskState("graded"))
      .mockResolvedValue(taskState("graded"));

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

    render(<MemoryRouter><FrontierLiveDemoPage /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: /start real OCR \+ grading/i }));

    await waitFor(() => expect(extractProblems).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(updateProblem).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(parseSubmissions).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(saveGradingSetup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(startGrading).toHaveBeenCalledTimes(1));
    await screen.findByText("16 answer units processed");
    expect(createTask).toHaveBeenCalledTimes(1);
    expect(extractProblems).toHaveBeenCalledTimes(1);
    expect(updateProblem).toHaveBeenCalledTimes(4);
    expect(parseSubmissions).toHaveBeenCalledTimes(1);
    expect(saveGradingSetup).toHaveBeenCalledTimes(1);
    expect(startGrading).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/static score has been substituted/i)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/provider Demo Vision/)).toBeInTheDocument());
  });
});

function taskState(status: TaskStateSnapshot["status"]): TaskStateSnapshot {
  return {
    task_id: "asg_demo123",
    name: "AWS Frontier Live Demo",
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

function taskWithQuestions(): Task {
  const base = taskState("problems_ready");
  const problem = (qId: string, number: string, stem: string) => ({
    q_id: qId,
    number,
    type: "calculation",
    stem,
    criterion: "",
    max_score: 10,
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

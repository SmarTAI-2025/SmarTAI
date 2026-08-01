import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TeacherAssignmentDetailPage } from "@/routes/teacher/TeacherAssignmentDetailPage";
import type { Assignment, GradingRun, Question } from "@/types/education";

vi.mock("@/api/hooks/education", () => ({
  useAssignment: vi.fn(),
  useQuestions: vi.fn(),
  useGradingRuns: vi.fn(),
  useAddQuestion: vi.fn(),
  usePublishAssignment: vi.fn(),
  useStartGradingRun: vi.fn(),
}));

const { useAssignment, useQuestions, useGradingRuns, useAddQuestion, usePublishAssignment, useStartGradingRun } =
  await import("@/api/hooks/education");

function renderPage(assignmentId = "a1") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <TeacherAssignmentDetailPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const draft: Assignment = {
  id: "a1", course_id: "c1", teacher_id: "t1", name: "第一次作业", description: "",
  status: "draft", due_at: null, created_at: 1, updated_at: 1, published_at: null,
  version: 1, question_count: 0,
};
const published: Assignment = { ...draft, status: "published", version: 2, published_at: 3, question_count: 1 };
const questions: Question[] = [
  { id: "q1", assignment_id: "a1", q_id: "q1", order_index: 0, number: "", type: "short",
    stem: "1+1=?", criterion: "", max_score: 10, reference_answer: null, test_cases: null,
    source: null, version: 1, created_at: 1, updated_at: 1 },
];
const runs: GradingRun[] = [];

function mock(opts: { assignment?: Assignment; questions?: Question[]; state?: "loading" | "error" | "ok" }) {
  const state = opts.state ?? "ok";
  (useAssignment as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? (opts.assignment ?? draft) : undefined,
    isLoading: state === "loading",
    isError: state === "error",
  });
  (useQuestions as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? (opts.questions ?? []) : undefined,
    isLoading: state === "loading",
    isError: false,
  });
  (useGradingRuns as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: runs, isLoading: false });
  (useAddQuestion as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
  (usePublishAssignment as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
  (useStartGradingRun as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
}

describe("TeacherAssignmentDetailPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("disables publish when the assignment has no questions", () => {
    mock({ assignment: draft, questions: [] });
    renderPage();
    expect(screen.getByRole("button", { name: "发布作业" })).toBeDisabled();
  });

  it("disables publish for a published (frozen) assignment", () => {
    mock({ assignment: published, questions });
    renderPage();
    expect(screen.getByRole("button", { name: "发布作业" })).toBeDisabled();
  });

  it("enables publish once a draft has questions", () => {
    mock({ assignment: draft, questions });
    renderPage();
    expect(screen.getByRole("button", { name: "发布作业" })).not.toBeDisabled();
  });

  it("add-question form is wired: filling q_id enables the submit button", async () => {
    mock({ assignment: draft, questions: [] });
    const user = userEvent.setup();
    renderPage();
    const submit = screen.getByRole("button", { name: "添加题目" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByPlaceholderText("q1"), "q1");
    expect(submit).not.toBeDisabled();
  });

  it("hides the add-question form once the assignment is published", () => {
    mock({ assignment: published, questions });
    renderPage();
    expect(screen.queryByRole("button", { name: "添加题目" })).not.toBeInTheDocument();
  });
});

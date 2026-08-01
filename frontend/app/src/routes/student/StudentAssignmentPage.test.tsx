import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StudentAssignmentPage } from "@/routes/student/StudentAssignmentPage";
import type { Assignment, GradeResult, Question } from "@/types/education";

vi.mock("@/api/hooks/education", () => ({
  useAssignment: vi.fn(),
  useQuestions: vi.fn(),
  useMyStudentResult: vi.fn(),
  useSubmitOnline: vi.fn(),
  useUploadSubmission: vi.fn(),
}));

const {
  useAssignment,
  useQuestions,
  useMyStudentResult,
  useSubmitOnline,
  useUploadSubmission,
} = await import("@/api/hooks/education");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <StudentAssignmentPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const published: Assignment = {
  id: "a1", course_id: "c1", teacher_id: "t1", name: "第一次作业", description: "",
  status: "published", due_at: null, created_at: 1, updated_at: 1, published_at: 2,
  version: 2, question_count: 1,
};
const closed: Assignment = { ...published, status: "closed" };
const questions: Question[] = [
  { id: "q1", assignment_id: "a1", q_id: "q1", order_index: 0, number: "", type: "short",
    stem: "1+1=?", criterion: "", max_score: 10, reference_answer: null, test_cases: null,
    source: null, version: 1, created_at: 1, updated_at: 1 },
];
const released: GradeResult[] = [
  { id: "gr1", grading_run_id: "r1", question_id: "q1", q_id: "q1", student_id: "s1",
    ai_score: 9, ai_max_score: 10, ai_comment: "ok", result_status: "graded",
    requires_review: false, review_reason: null, score: 9, teacher_comment: "" },
];

function mock(opts: {
  assignment?: Assignment;
  questions?: Question[];
  result?: GradeResult[];
  state?: "loading" | "error" | "ok";
}) {
  const state = opts.state ?? "ok";
  (useAssignment as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? (opts.assignment ?? published) : undefined,
    isLoading: state === "loading",
    isError: state === "error",
  });
  (useQuestions as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? (opts.questions ?? questions) : undefined,
    isLoading: false,
  });
  (useMyStudentResult as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? (opts.result ?? []) : undefined,
    isLoading: false,
  });
  (useSubmitOnline as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: vi.fn(), isPending: false });
  (useUploadSubmission as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    data: undefined,
  });
}

describe("StudentAssignmentPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the answer form for an open assignment", () => {
    mock({ assignment: published });
    renderPage();
    expect(screen.getByText("1+1=?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交作答" })).toBeInTheDocument();
  });

  it("hides the answer form when the assignment is closed", () => {
    mock({ assignment: closed });
    renderPage();
    expect(screen.queryByRole("button", { name: "提交作答" })).not.toBeInTheDocument();
  });

  it("submits answers keyed by q_id", async () => {
    mock({ assignment: published });
    const submit = vi.fn();
    (useSubmitOnline as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutate: submit, isPending: false });
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByRole("textbox"), "2");
    await user.click(screen.getByRole("button", { name: "提交作答" }));
    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({ assignmentId: "a1", answers: expect.arrayContaining([expect.objectContaining({ q_id: "q1", content: "2" })]) }),
      expect.any(Object),
    );
  });

  it("uploads a handwritten submission for OCR", async () => {
    mock({ assignment: published });
    const upload = vi.fn();
    (useUploadSubmission as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutate: upload,
      isPending: false,
      data: undefined,
    });
    const user = userEvent.setup();
    renderPage();
    const file = new File(["image"], "homework.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("上传手写作业"), file);
    await user.click(screen.getByRole("button", { name: "识别并提交" }));
    expect(upload).toHaveBeenCalledWith(
      { assignmentId: "a1", file },
      expect.any(Object),
    );
  });

  it("shows released results once available, nothing before release", () => {
    mock({ assignment: published, result: [] });
    const { rerender } = renderPage();
    expect(screen.getByText(/教师发布成绩后/)).toBeInTheDocument();
    mock({ assignment: published, result: released });
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <StudentAssignmentPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/9\s*\/\s*10/)).toBeInTheDocument();
  });
});

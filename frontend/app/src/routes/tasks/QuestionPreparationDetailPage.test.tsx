import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceFileDescriptor } from "@/types/sourcePreview";
import { QuestionPreparationDetailPage } from "./QuestionPreparationDetailPage";

const testState = vi.hoisted(() => ({ locale: "zh-CN" }));
const mutateAsync = vi.hoisted(() => vi.fn());
const sourcePreviewApi = vi.hoisted(() => ({
  getTaskSourceFiles: vi.fn(),
  loadSourcePreviewFile: vi.fn(),
}));
const problemSource: SourceFileDescriptor = {
  source_id: "source-problem-1",
  file_id: "file-problem-1",
  display_name: "geometry-problems.pdf",
  mime_type: "application/pdf",
  size_bytes: 42,
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};
const taskData = vi.hoisted(() => ({
  task_id: "task-1",
  name: "Geometry",
  status: "problems_ready",
  workflow_revision: 7,
  problem_file_name: "geometry-problems.pdf",
  problem_data: {
    Q1: {
      q_id: "Q1",
      number: "1",
      type: "Proof",
      stem: "Proof question one",
      max_score: 10,
      max_score_source: "default_10",
      max_score_review_status: "needs_review",
      criterion: "Rubric one",
      reference_answer: "Answer one",
      preparation_issues: [],
    },
    Q2: {
      q_id: "Q2",
      number: "2",
      type: "Proof",
      stem: "Proof question two",
      max_score: 10,
      criterion: "Rubric two",
      reference_answer: "Answer two",
      preparation_issues: [],
    },
    Q3: {
      q_id: "Q3",
      number: "3",
      type: "编程题",
      stem: "请使用 Python 编写 Fibonacci 函数，并返回第 n 项。",
      max_score: 10,
      criterion: "正确处理边界条件并返回结果。",
      reference_answer: "使用迭代方式保存相邻两项。",
      solution_code: "def fibonacci(n):\n    if n <= 0: return 0\n    if n == 1: return 1\n    a, b = 0, 1\n    for _ in range(2, n + 1):\n        a, b = b, a + b\n    return b",
      test_cases: [{
        input: "0",
        expected_output: "0",
        description: "边界条件",
        visibility: "example",
        source: "teacher",
        sandbox_feasible: true,
      }],
      preparation_issues: [],
    },
    Q4: {
      q_id: "Q4",
      number: "4",
      type: "Programming",
      stem: "Implement binary search in Java and return the target index.",
      max_score: 10,
      criterion: "Use binary search and return -1 when the target is absent.",
      reference_answer: "Maintain inclusive low and high bounds.",
      solution_code: "public static int binarySearch(int[] values, int target) {\n    int low = 0, high = values.length - 1;\n    while (low <= high) {\n        int middle = low + (high - low) / 2;\n        if (values[middle] == target) return middle;\n        if (values[middle] < target) low = middle + 1;\n        else high = middle - 1;\n    }\n    return -1;\n}",
      test_cases: [{
        input: "[1, 3, 5], 3",
        expected_output: "1",
        description: "Target is present",
        visibility: "example",
        source: "teacher",
        sandbox_feasible: false,
      }],
      preparation_issues: [],
    },
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({
    isLoading: false,
    isError: false,
    data: taskData,
  }),
  useUpdateProblem: () => ({ isPending: false, mutateAsync }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/api/sourcePreview", () => ({
  getTaskSourceFiles: sourcePreviewApi.getTaskSourceFiles,
  loadSourcePreviewFile: sourcePreviewApi.loadSourcePreviewFile,
  sourcePreviewErrorCode: () => "source_preview_load_failed",
}));

vi.mock("@/components/tasks/PdfDocumentPreview", () => ({
  PdfDocumentPreview: ({ url, title }: { url: string; title: string }) => (
    <object data={url} type="application/pdf" title={title} />
  ),
}));

vi.mock("@/i18n/I18nProvider", async () => {
  const { messages } = await vi.importActual<typeof import("@/i18n/messages")>("@/i18n/messages");
  return {
    useI18n: () => ({
      locale: testState.locale,
      t: (key: keyof typeof messages["zh-CN"]) => messages[testState.locale as "zh-CN" | "en-US"][key],
    }),
  };
});

function renderPage(initialEntry = "/tasks/task-1/questions/Q1/content") {
  const router = createMemoryRouter([
    {
      path: "/tasks/:taskId/questions/:questionId/:section",
      element: <QuestionPreparationDetailPage />,
    },
    {
      path: "/tasks/:taskId/questions",
      element: <div>Question overview destination</div>,
    },
    {
      path: "/tasks/:taskId/submissions/upload",
      element: <div>Submission upload destination</div>,
    },
  ], { initialEntries: [initialEntry] });

  render(<RouterProvider router={router} />);
  return router;
}

beforeEach(() => {
  testState.locale = "zh-CN";
  mutateAsync.mockReset();
  sourcePreviewApi.getTaskSourceFiles.mockReset().mockResolvedValue({
    task_id: "task-1",
    workflow_revision: 7,
    problem_source: problemSource,
    submission_sources: {},
  });
  sourcePreviewApi.loadSourcePreviewFile.mockReset().mockResolvedValue(
    new Blob(["pdf"], { type: "application/pdf" }),
  );
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:problem-source") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  Object.defineProperty(window, "scrollTo", { configurable: true, value: vi.fn() });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function getRect(this: HTMLElement) {
    const top = this.id === "question-Q2" ? 600 : 0;
    return {
      x: 0,
      y: top,
      top,
      right: 100,
      bottom: top + 100,
      left: 0,
      width: 100,
      height: 100,
      toJSON: () => ({}),
    };
  });
});

describe("QuestionPreparationDetailPage navigation", () => {
  it("opens the 50/50 workspace with the formal problem_source descriptor", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看题目原文件" }));
    expect(await screen.findByTestId("source-preview-panel")).toBeInTheDocument();
    expect(screen.getByRole("separator", { name: "拖动调整原文件与识别内容宽度" })).toHaveAttribute("aria-valuenow", "50");
    await waitFor(() => expect(sourcePreviewApi.getTaskSourceFiles).toHaveBeenCalledWith("task-1"));
    await waitFor(() => expect(sourcePreviewApi.loadSourcePreviewFile).toHaveBeenCalledWith("task-1", problemSource));
    expect(await screen.findByTitle("原文件 · geometry-problems.pdf")).toHaveAttribute("data", "blob:problem-source");
  });

  it("uses a bounded responsive question rail and preserves the full label on hover", async () => {
    renderPage();

    const navigation = await screen.findByRole("complementary", { name: "题目导航" });
    expect(navigation.parentElement).toHaveClass("lg:grid-cols-[clamp(180px,16vw,240px)_minmax(0,1fr)]");
    expect(screen.getByRole("button", { name: "第 1 题" }).querySelector(".truncate")).toHaveAttribute("title", "第 1 题");
  });

  it("shows and edits the authoritative maximum score", async () => {
    const user = userEvent.setup();
    renderPage();

    expect((await screen.findAllByText("本题满分")).length).toBeGreaterThan(0);
    expect(screen.getByText(/系统暂按默认 10 分/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "修改第 1 题满分" }));
    const input = screen.getByRole("spinbutton", { name: "第 1 题满分" });
    await user.clear(input);
    await user.type(input, "5");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
      taskId: "task-1",
      qId: "Q1",
      expectedWorkflowRevision: 7,
      max_score: 5,
    }));
  });

  it("carries the latest workflow revision across different maximum-score edits and final confirmation", async () => {
    let workflowRevision = taskData.workflow_revision;
    mutateAsync.mockImplementation(async () => ({
      workflow_revision: ++workflowRevision,
    }));
    const user = userEvent.setup();
    const router = renderPage();

    await user.click(await screen.findByRole("button", { name: "修改第 1 题满分" }));
    const firstScore = screen.getByRole("spinbutton", { name: "第 1 题满分" });
    await user.clear(firstScore);
    await user.type(firstScore, "5");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await user.click(screen.getByRole("button", { name: "修改第 2 题满分" }));
    const secondScore = screen.getByRole("spinbutton", { name: "第 2 题满分" });
    await user.clear(secondScore);
    await user.type(secondScore, "15");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await user.click(screen.getByRole("button", { name: "确认全部题目资料" }));

    expect(await screen.findByText("Submission upload destination")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/tasks/task-1/submissions/upload");
    expect(mutateAsync.mock.calls.map(([input]) => input.expectedWorkflowRevision)).toEqual([
      7, 8, 9, 10, 11, 12,
    ]);
  });

  it("does not block navigation only because the default maximum score needs review", async () => {
    const user = userEvent.setup();
    const router = renderPage();

    await user.click(await screen.findByRole("link", { name: "返回题目资料总览" }));

    expect(await screen.findByText("Question overview destination")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/tasks/task-1/questions");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("blocks navigation only after the teacher makes an unsaved maximum-score edit", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "修改第 1 题满分" }));
    const input = screen.getByRole("spinbutton", { name: "第 1 题满分" });
    await user.clear(input);
    await user.type(input, "8");
    await user.click(screen.getByRole("link", { name: "返回题目资料总览" }));

    expect(await screen.findByRole("alertdialog", { name: "离开且不保存？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续编辑" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃修改" })).toBeInTheDocument();
  });

  it("uses reviewed English copy and returns to the question material overview", async () => {
    testState.locale = "en-US";
    const user = userEvent.setup();
    const router = renderPage("/tasks/task-1/questions/Q1/content?q=proof");

    const backLink = await screen.findByRole("link", { name: "Back to Question Material Overview" });
    expect(backLink).toHaveAttribute("href", "/tasks/task-1/questions?q=proof");
    expect(screen.getAllByText("LaTeX and code are rendered while browsing; click Edit to edit the source.")).toHaveLength(2);

    await user.click(backLink);

    expect(await screen.findByText("Question overview destination")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/tasks/task-1/questions");
    expect(router.state.location.search).toBe("?q=proof");
  });

  it("switches questions with ArrowDown and ArrowUp but not while an input is focused", async () => {
    renderPage();
    const firstQuestion = await screen.findByRole("button", { name: "第 1 题" });
    const secondQuestion = screen.getByRole("button", { name: "第 2 题" });

    expect(screen.getAllByText("浏览态会渲染 LaTeX 或代码；点击修改可编辑源码。")).toHaveLength(4);
    expect(screen.getByLabelText("代码语言：Python")).toBeInTheDocument();
    expect(screen.getByLabelText("代码语言：Java")).toBeInTheDocument();

    await waitFor(() => expect(firstQuestion).toHaveAttribute("aria-current", "true"));

    vi.mocked(window.scrollTo).mockClear();
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(window.scrollTo).toHaveBeenCalled();
    await waitFor(() => expect(secondQuestion).toHaveAttribute("aria-current", "true"));

    fireEvent.keyDown(window, { key: "ArrowUp" });
    await waitFor(() => expect(firstQuestion).toHaveAttribute("aria-current", "true"));

    const search = screen.getByRole("textbox", { name: "本地快速筛选题目，不调用模型" });
    search.focus();
    fireEvent.keyDown(search, { key: "ArrowDown" });

    expect(firstQuestion).toHaveAttribute("aria-current", "true");
    expect(secondQuestion).not.toHaveAttribute("aria-current");
  });

  it("keeps the prepared Python and Java questions as language-specific highlighting fixtures", async () => {
    testState.locale = "en-US";
    renderPage();

    const pythonBadge = await screen.findByLabelText("Code language: Python");
    const javaBadge = screen.getByLabelText("Code language: Java");

    expect(pythonBadge.closest("[data-code-language]")).toHaveAttribute("data-code-language", "python");
    expect(javaBadge.closest("[data-code-language]")).toHaveAttribute("data-code-language", "java");
    expect(document.querySelectorAll("[data-code-token='keyword']").length).toBeGreaterThan(0);
  });
});

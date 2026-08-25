import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { AddProblemsPage } from "./AddProblemsPage";

const preflightMutateAsync = vi.hoisted(() => vi.fn());
const startMutateAsync = vi.hoisted(() => vi.fn());
const taskRefetch = vi.hoisted(() => vi.fn());
const expertsRefetch = vi.hoisted(() => vi.fn());
const capabilityState = vi.hoisted(() => ({
  available: true,
  data: {
    source_roles: {
      problem: { accepted_extensions: [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"] },
      reference_answer: { accepted_extensions: [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"] },
      rubric: { accepted_extensions: [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"] },
      programming_tests: { accepted_extensions: [".pdf", ".txt", ".md", ".markdown", ".json"] },
    },
    reader: { ocr: true },
    limits: { max_file_bytes: 5 * 1024 * 1024 },
    score_policy: {
      maximum_max_score: 10_000,
      per_question_text_max_characters: 12_000,
    },
  },
}));

vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({
    data: [{
      provider_id: "mock:test",
      provider_type: "openai",
      model: "test-model",
      enabled: true,
      is_default: true,
    }],
    isLoading: false,
    isError: false,
    refetch: expertsRefetch,
  }),
  useProblemSourceLibrary: () => ({
    data: { items: [] },
    isFetching: false,
  }),
  useProblemSourcePreflight: () => ({
    isPending: false,
    mutateAsync: preflightMutateAsync,
  }),
  useQuestionPreparationCapabilities: () => ({
    data: capabilityState.available ? capabilityState.data : undefined,
  }),
  useStartQuestionPreparation: () => ({
    isPending: false,
    mutateAsync: startMutateAsync,
  }),
  useTask: () => ({
    isSuccess: true,
    data: {
      task_id: "task-1",
      name: "Assignment",
      status: "draft",
      workflow_revision: 0,
      problem_count: 0,
      problem_file_name: null,
      course_id: "course-1",
    },
    refetch: taskRefetch,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

function renderPage() {
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/upload/problems", element: <AddProblemsPage /> },
    { path: "/tasks/:taskId/problems/progress", element: <div>Preparation started</div> },
  ], { initialEntries: ["/tasks/task-1/upload/problems"] });
  render(<RouterProvider router={router} />);
  return router;
}

async function uploadProblemFile(user: ReturnType<typeof userEvent.setup>) {
  const file = new File(["1. Explain dependency injection"], "questions.pdf", {
    type: "application/pdf",
  });
  await user.upload(screen.getByLabelText("选择文件"), file);
}

beforeEach(() => {
  capabilityState.available = true;
  capabilityState.data.source_roles.problem.accepted_extensions = [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"];
  capabilityState.data.source_roles.reference_answer.accepted_extensions = [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"];
  capabilityState.data.source_roles.rubric.accepted_extensions = [".pdf", ".txt", ".md", ".markdown", ".jpg", ".jpeg", ".png", ".webp"];
  capabilityState.data.source_roles.programming_tests.accepted_extensions = [".pdf", ".txt", ".md", ".markdown", ".json"];
  capabilityState.data.reader.ocr = true;
  capabilityState.data.limits.max_file_bytes = 5 * 1024 * 1024;
  preflightMutateAsync.mockReset();
  startMutateAsync.mockReset();
  taskRefetch.mockReset();
  expertsRefetch.mockReset();
  preflightMutateAsync.mockResolvedValue({ source_token: "source-1" });
  startMutateAsync.mockResolvedValue({ status: "started", job_id: "job-1" });
  taskRefetch.mockResolvedValue({ data: { status: "error" } });
  expertsRefetch.mockResolvedValue({ data: [] });
});

describe("AddProblemsPage score configuration", () => {
  it("keeps the explicit default-10 contract when the teacher does not edit scores", async () => {
    const user = userEvent.setup();
    renderPage();
    await uploadProblemFile(user);

    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));

    await waitFor(() => expect(startMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      scorePolicy: { mode: "default_10" },
      recognitionProviderId: "mock:test",
    })));
    expect(preflightMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      recognitionProviderId: "mock:test",
    }));
  });

  it("sends an explicitly edited uniform maximum score", async () => {
    const user = userEvent.setup();
    renderPage();
    await uploadProblemFile(user);

    await user.click(screen.getByRole("button", { name: /3\. 评分标准/ }));
    const scoreInput = screen.getByRole("spinbutton", { name: "每题满分" });
    expect(scoreInput).toHaveValue(10);
    await user.clear(scoreInput);
    await user.type(scoreInput, "5");
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));

    await waitFor(() => expect(startMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      scorePolicy: { mode: "uniform", uniformMaxScore: 5 },
    })));
    expect(await screen.findByText("Preparation started")).toBeInTheDocument();
  });

  it("sends per-question natural-language score instructions", async () => {
    const user = userEvent.setup();
    renderPage();
    await uploadProblemFile(user);

    await user.click(screen.getByRole("button", { name: /3\. 评分标准/ }));
    await user.click(screen.getByRole("checkbox", { name: "每题满分不同" }));
    await user.type(
      screen.getByRole("textbox", { name: "每题满分说明" }),
      "第 1 题 5 分，第 2 题 15 分",
    );
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));

    await waitFor(() => expect(startMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      scorePolicy: {
        mode: "per_question",
        perQuestionText: "第 1 题 5 分，第 2 题 15 分",
      },
    })));
  });
});

describe("AddProblemsPage upload capability contract", () => {
  it("shows and enforces the backend file-size limit before preflight", () => {
    renderPage();
    expect(screen.getByText(/单个文件最大 5\.0 MB/)).toBeInTheDocument();

    const oversized = new File(["x"], "oversized.pdf", { type: "application/pdf" });
    Object.defineProperty(oversized, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(screen.getByLabelText("选择文件"), {
      target: { files: [oversized] },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("超过单个文件 5.0 MB 的上传上限");
    expect(preflightMutateAsync).not.toHaveBeenCalled();
  });

  it("accepts a question image from the chooser when vision capability allows it", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = screen.getByLabelText("选择文件");

    expect(input).toHaveAttribute("accept", expect.stringContaining(".png"));
    await user.upload(input, new File(["image"], "questions.png", { type: "image/png" }));

    expect(screen.getAllByText("questions.png")).toHaveLength(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps images selectable and lets the selected provider return the precise capability error", () => {
    capabilityState.data.source_roles.problem.accepted_extensions = [".pdf", ".txt", ".md", ".markdown"];
    capabilityState.data.reader.ocr = false;
    renderPage();
    const image = new File(["image"], "questions.png", { type: "image/png" });
    const input = screen.getByLabelText("选择文件");

    expect(input).toHaveAttribute("accept", expect.stringContaining(".png"));
    fireEvent.change(input, { target: { files: [image] } });
    expect(screen.getAllByText("questions.png")).toHaveLength(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(preflightMutateAsync).not.toHaveBeenCalled();
  });

  it("keeps supported image formats selectable while capability data is loading", () => {
    capabilityState.available = false;
    renderPage();

    const input = screen.getByLabelText("选择文件");
    expect(input).toHaveAttribute("accept", expect.stringContaining(".webp"));
    fireEvent.drop(screen.getByLabelText("题目来源文件上传"), {
      dataTransfer: {
        files: [new File(["image"], "questions.webp", { type: "image/webp" })],
      },
    });
    expect(screen.getAllByText("questions.webp")).toHaveLength(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps programming-test images blocked even when question OCR is available", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /4\. 测试样例/ }));
    await user.click(screen.getByRole("button", { name: "再添加一份测试资料" }));

    fireEvent.drop(screen.getByLabelText("测试资料来源文件上传"), {
      dataTransfer: {
        files: [new File(["image"], "cases.png", { type: "image/png" })],
      },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("编程题测试资料不接受图片");
  });
});

describe("AddProblemsPage workflow recovery", () => {
  it("refreshes the server snapshot and dismisses a stale workflow-busy warning", async () => {
    const user = userEvent.setup();
    startMutateAsync.mockRejectedValueOnce(new APIError(
      409,
      "The task is busy.",
      { detail: { code: "workflow_busy", stage: "question_preparation" } },
    ));
    renderPage();
    await uploadProblemFile(user);

    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await user.click(await screen.findByRole("button", { name: "刷新任务状态" }));

    await waitFor(() => expect(taskRefetch).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: "刷新任务状态" })).not.toBeInTheDocument();
  });
});

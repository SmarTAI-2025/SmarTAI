import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, Outlet } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import * as client from "@/api/client";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { rememberImageReturn } from "@/lib/imageRecoveryNavigation";
import * as draftStore from "@/lib/pageDraftStore";
import { clearPageDrafts } from "@/lib/pageDraftStore";
import { AddProblemsPage } from "./AddProblemsPage";

const preflightMutateAsync = vi.hoisted(() => vi.fn());
const startMutateAsync = vi.hoisted(() => vi.fn());
const taskRefetch = vi.hoisted(() => vi.fn());
const expertsRefetch = vi.hoisted(() => vi.fn());
const taskState = vi.hoisted(() => ({ status: "draft", last_failed_job_id: null as string | null, extract_job_id: null as string | null }));
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
const providerState = vi.hoisted(() => ({ enabled: true, imageState: "unverified" }));

const inputState = vi.hoisted(() => ({ input: null as unknown }));
vi.mock("@/api/workflowInputs", () => ({ useWorkflowInput: () => ({ data: { input: inputState.input }, isError: false, isLoading: false }) }));
vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({
    data: providerState.enabled ? [{
      provider_id: "mock:test",
      provider_type: "openai",
      model: "test-model",
      enabled: true,
      is_default: true,
      image_capability_status: providerState.imageState,
    }, { provider_id: "new:model", model: "new-model", provider_type: "qwen", enabled: true }] : [],
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
      ...taskState,
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

function renderPage(owner = "draft-teacher", priorModel?: string) {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [
    { path: "/tasks/:taskId/upload/problems", element: <AddProblemsPage /> },
    { path: "/tasks/:taskId/problems/progress", element: <div>Preparation started</div> },
    { path: "/settings/byok", element: <div>BYOK configuration</div> },
  ] }], { initialEntries: [priorModel ? { pathname: "/tasks/task-1/upload/problems", state: { imageRecoveryModel: priorModel } } : "/tasks/task-1/upload/problems"] });
  render(<PageDraftSession ownerId={owner}><RouterProvider router={router} /></PageDraftSession>);
  return router;
}

async function uploadProblemFile(user: ReturnType<typeof userEvent.setup>) {
  const file = new File(["1. Explain dependency injection"], "questions.pdf", {
    type: "application/pdf",
  });
  await user.upload(screen.getByLabelText("选择文件"), file);
  if (providerState.enabled) await waitFor(() => expect(screen.getByLabelText("题目识别模型")).toHaveValue("mock:test"));
}

beforeEach(async () => {
  inputState.input = null;
  vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File);
  await clearPageDrafts();
  providerState.enabled = true;
  providerState.imageState = "unverified";
  vi.spyOn(client, "getJSON").mockResolvedValue({ available: true, prepared: true, filename: "questions.pdf" });
  Object.assign(taskState, { status: "draft", last_failed_job_id: null, extract_job_id: null });
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
  it.each([
    ["", "", ""],
    ["", "1.1.5, 1.1.7, 1.1.20, 1.1.29, 1.1.31, 1.2.3, 1.2.16", ""],
    ["3-5", "", ""],
    ["", "", "仅提取第一节的习题"],
  ])("allows independently omitted scope fields (%s, %s, %s)", async (pages, targets, hint) => {
    const user = userEvent.setup();
    renderPage();
    await uploadProblemFile(user);
    await user.click(screen.getByRole("button", { name: "从原文提取" }));
    fireEvent.change(screen.getByLabelText("页码（选填）"), { target: { value: pages } });
    fireEvent.change(screen.getByLabelText("目标题号（选填）"), { target: { value: targets } });
    fireEvent.change(screen.getByLabelText("补充说明（选填）"), { target: { value: hint } });
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await waitFor(() => expect(startMutateAsync).toHaveBeenCalledTimes(1));
    expect(preflightMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      recognitionOptions: { pages: pages ? [3, 4, 5] : [], targets: targets ? targets.split(", ") : [] },
    }));
  });

  it("explains malformed pages before any upload/model request", async () => {
    const user = userEvent.setup();
    renderPage();
    await uploadProblemFile(user);
    await user.click(screen.getByRole("button", { name: "从原文提取" }));
    fireEvent.change(screen.getByLabelText("页码（选填）"), { target: { value: "5-2" } });
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    expect(screen.getByText(/页码请填写 PDF 文件页序号/)).toBeInTheDocument();
    expect(preflightMutateAsync).not.toHaveBeenCalled();
    expect(startMutateAsync).not.toHaveBeenCalled();
  });

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
  it("links back to the retained preparation after reopening the upload page", async () => {
    Object.assign(taskState, { status: "error", last_failed_job_id: "failed-job", extract_job_id: "failed-job" });
    renderPage();
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("已上传资料仍保留");
    fireEvent.click(screen.getByRole("link", { name: "返回进度并重试" }));
    expect(await screen.findByText("Preparation started")).toBeInTheDocument();
    expect(preflightMutateAsync).not.toHaveBeenCalled();
  });

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
    await user.click(await screen.findByRole("button", { name: "刷新任务状态" }, { timeout: 3000 }));

    await waitFor(() => expect(taskRefetch).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: "刷新任务状态" })).not.toBeInTheDocument();
  });
});


it("rejects an empty file before preflight and accepts a corrected replacement", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.upload(screen.getByLabelText("选择文件"), new File([], "empty.txt", { type: "text/plain" }));
  expect(screen.getByText(/文件为空（0 字节）/)).toBeInTheDocument();
  expect(preflightMutateAsync).not.toHaveBeenCalled();
  await user.upload(screen.getByLabelText("选择文件"), new File(["1. Calculate 2+3"], "fixed.txt", { type: "text/plain" }));
  expect(screen.queryByText(/文件为空（0 字节）/)).not.toBeInTheDocument();
});

describe("page draft navigation", () => {
  it("keeps a local file, scope, score options and step through BYOK, back and forward without requests", async () => {
    providerState.enabled = false;
    const user = userEvent.setup(); const router = renderPage();
    await uploadProblemFile(user);
    await user.click(screen.getByRole("button", { name: "从原文提取" }));
    fireEvent.change(screen.getByLabelText("页码（选填）"), { target: { value: "3-5" } });
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await user.click(screen.getByRole("link", { name: "前往 BYOK" }));
    await user.click(await screen.findByRole("button", { name: "暂存并离开" }));
    expect(await screen.findByText("BYOK configuration")).toBeInTheDocument();
    await act(async () => { await router.navigate(-1); });
    await waitFor(() => expect(screen.getByLabelText("页码（选填）")).toHaveValue("3-5"));
    expect(screen.getAllByText("questions.pdf").length).toBeGreaterThan(0);
    await act(async () => { await router.navigate(1); await router.navigate(-1); });
    await waitFor(() => expect(screen.getByLabelText("页码（选填）")).toHaveValue("3-5"));
    expect(preflightMutateAsync).not.toHaveBeenCalled(); expect(startMutateAsync).not.toHaveBeenCalled();
    providerState.enabled = true;
  providerState.imageState = "unverified";
    await act(async () => { await router.navigate("/settings/byok"); await router.navigate(-1); });
    await saveDraft();
    await act(async () => { await router.navigate("/settings/byok"); await router.navigate(-1); });
    await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await waitFor(() => expect(preflightMutateAsync).toHaveBeenCalledTimes(1));
    expect(preflightMutateAsync.mock.calls[0][0].file).toBeInstanceOf(File);
  });

  it("reuses a successful preflight after returning and clears on submit", async () => {
    const user = userEvent.setup(); const router = renderPage();
    preflightMutateAsync.mockResolvedValue({ source_token: "prepared-1", source: { stored_file_id: "file-1" } });
    startMutateAsync.mockRejectedValueOnce(new APIError(503, "try later"));
    await uploadProblemFile(user);
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await waitFor(() => expect(startMutateAsync).toHaveBeenCalledTimes(1));
    await act(async () => { await router.navigate("/settings/byok"); await router.navigate(-1); });
    await waitFor(() => expect(screen.getByRole("button", { name: "识别并准备题目资料" })).toBeEnabled());
    expect(preflightMutateAsync).toHaveBeenCalledTimes(1); expect(startMutateAsync).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    expect(await screen.findByText("Preparation started")).toBeInTheDocument();
    expect(preflightMutateAsync).toHaveBeenCalledTimes(1); expect(startMutateAsync).toHaveBeenCalledTimes(2);
    await act(async () => { await router.navigate(-1); });
    expect(screen.queryByText("questions.pdf")).not.toBeInTheDocument();
  });

  it("does not copy local files to another task and discards explicitly", async () => {
    const user = userEvent.setup(); const router = renderPage(); await uploadProblemFile(user); await saveDraft();
    await act(async () => { await router.navigate("/tasks/task-2/upload/problems"); });
    expect(screen.queryByText("questions.pdf")).not.toBeInTheDocument();
    await act(async () => { await router.navigate(-1); });
    await waitFor(() => expect(screen.getAllByText("questions.pdf").length).toBeGreaterThan(0));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "删除本页草稿" }));
    await waitFor(() => expect(screen.queryByText("questions.pdf")).not.toBeInTheDocument());
    await act(async () => { await router.navigate("/settings/byok"); await router.navigate(-1); });
    expect(screen.queryByText("questions.pdf")).not.toBeInTheDocument();
  });
});

async function saveDraft() { if (providerState.enabled) await waitFor(() => expect(screen.getByLabelText("题目识别模型")).toHaveValue("mock:test")); await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(screen.getByText(/已暂存 ·/)).toBeInTheDocument()); }


describe("image failure recovery uses explicit drafts", () => {
  it("saves actual file and scope, restores before applying latest model, then waits for manual action", async () => {
    const user = userEvent.setup();
    const router = renderPage();
    await uploadProblemFile(user);
    await user.click(screen.getByRole("button", { name: "从原文提取" }));
    fireEvent.change(screen.getByLabelText("页码（选填）"), { target: { value: "3-5" } });
    fireEvent.change(screen.getByLabelText("补充说明（选填）"), { target: { value: "保留这段说明" } });
    preflightMutateAsync.mockRejectedValueOnce(new APIError(422, "image_recognition_unconfirmed", { detail: { code: "image_recognition_unconfirmed" } }));
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await user.click(await screen.findByRole("button", { name: "暂存并去验证" }));
    await screen.findByText("BYOK configuration");
    expect(router.state.location.search).toContain("providerId=mock%3Atest");
    expect(preflightMutateAsync).toHaveBeenCalledTimes(1);
    rememberImageReturn("draft-teacher", "/tasks/task-1/upload/problems", "new:model");
    await act(async () => { await router.navigate("/tasks/task-1/upload/problems", { state: { imageRecoveryModel: "new:model" } }); });
    await waitFor(() => expect(screen.getByLabelText("题目识别模型")).toHaveValue("new:model"));
    expect(screen.getAllByText("questions.pdf").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("页码（选填）")).toHaveValue("3-5");
    expect(screen.getByLabelText("补充说明（选填）")).toHaveValue("保留这段说明");
    expect(preflightMutateAsync).toHaveBeenCalledTimes(1);
    preflightMutateAsync.mockResolvedValueOnce({ source_token: "new-source" });
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await waitFor(() => expect(preflightMutateAsync).toHaveBeenCalledTimes(2));
    const request = preflightMutateAsync.mock.calls[1][0];
    expect(request.recognitionProviderId).toBe("new:model");
    expect(await request.file.text()).toBe("1. Explain dependency injection");
  });
  it("stays with input when saving fails", async () => {
    const user = userEvent.setup(); renderPage(); await uploadProblemFile(user);
    preflightMutateAsync.mockRejectedValueOnce(new APIError(422, "provider_vision_not_supported", { detail: { code: "provider_vision_not_supported" } }));
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    await screen.findByRole("button", { name: "暂存并更换模型" });
    const spy = vi.spyOn(draftStore, "writePageDrafts").mockRejectedValueOnce(new Error("quota save failure"));
    await user.click(screen.getByRole("button", { name: "暂存并更换模型" }));
    expect((await screen.findAllByText("quota save failure")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("questions.pdf").length).toBeGreaterThan(0);
    expect(screen.queryByText("BYOK configuration")).not.toBeInTheDocument();
    spy.mockRestore();
  });
  it("passed model with poor recognition suggests clearer file without calling it unsupported", async () => {
    providerState.imageState = "passed";
    const user = userEvent.setup(); renderPage(); await uploadProblemFile(user);
    preflightMutateAsync.mockRejectedValueOnce(new APIError(422, "image_recognition_unconfirmed", { detail: { code: "image_recognition_unconfirmed" } }));
    await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
    expect(await screen.findByText(/所选模型已通过图片能力验证/)).toHaveTextContent("请换清晰文件或换模型");
    expect(screen.queryByRole("button", { name: "暂存并去验证" })).not.toBeInTheDocument();
    expect(screen.queryByText(/当前配置不支持图片输入/)).not.toBeInTheDocument();
  });
});


it("browser back keeps the latest explicit choice over old history state and saved draft", async () => {
  const user = userEvent.setup(); const router = renderPage("draft-teacher", "mock:test");
  await uploadProblemFile(user);
  preflightMutateAsync.mockRejectedValueOnce(new APIError(422, "image_recognition_unconfirmed", { detail: { code: "image_recognition_unconfirmed" } }));
  await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
  await user.click(await screen.findByRole("button", { name: "暂存并更换模型" }));
  await screen.findByText("BYOK configuration");
  rememberImageReturn("draft-teacher", "/tasks/task-1/upload/problems", "new:model");
  await act(async () => { await router.navigate(-1); });
  await waitFor(() => expect(screen.getByLabelText("题目识别模型")).toHaveValue("new:model"));
  expect(screen.getAllByText("questions.pdf").length).toBeGreaterThan(0);
  expect(preflightMutateAsync).toHaveBeenCalledTimes(1);
});


it("restores formally submitted files, scope, hints and policy without an explicit draft", async () => {
  inputState.input = { job_id: "prior", recognition_provider_id: "mock:test", score_policy: { mode: "per_question", per_question_text: "第 1 题 15 分" }, sources: [
    { source_token: "original-source", role: "problem", source_kind: "upload", filename: "saved.pdf", stored_file_id: "stored-1", library_material_id: null, inline_text: "", structure_mode: "extract_from_source", extraction_hint: "保留图表\n页码: 3-5\n题号: 1.1.5", recognition_options: { pages: [3, 4, 5], targets: ["1.1.5"] }, enable_material_ocr: false, save_to_library: true, available: true },
    { source_token: "answer-source", role: "reference_answer", source_kind: "inline_text", filename: "answer.txt", stored_file_id: null, library_material_id: null, inline_text: "完整参考解答", structure_mode: "organized", extraction_hint: "", recognition_options: {}, enable_material_ocr: false, save_to_library: false, available: true },
  ] };
  const router = renderPage();
  await screen.findAllByText("saved.pdf");
  await waitFor(() => expect(screen.getByLabelText("题目识别模型")).toHaveValue("mock:test"));
  expect(screen.getByLabelText("页码（选填）")).toHaveValue("3, 4, 5");
  expect(screen.getByLabelText("目标题号（选填）")).toHaveValue("1.1.5");
  expect(screen.getByLabelText("补充说明（选填）")).toHaveValue("保留图表");
  expect(preflightMutateAsync).not.toHaveBeenCalled();
  expect(startMutateAsync).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
  await screen.findByText("Preparation started");
  expect(preflightMutateAsync.mock.calls.map(([input]) => input.role)).toEqual(["problem", "reference_answer"]);
  expect(preflightMutateAsync.mock.calls[0][0]).toMatchObject({ storedFileId: "stored-1", file: null, recognitionOptions: { pages: [3, 4, 5], targets: ["1.1.5"] } });
  expect(startMutateAsync.mock.calls[0][0].scorePolicy).toEqual({ mode: "per_question", perQuestionText: "第 1 题 15 分" });
  // Returning through browser history reloads server inputs, without a model call.
  await act(async () => { await router.navigate(-1); });
  await screen.findAllByText("saved.pdf");
  expect(preflightMutateAsync).toHaveBeenCalledTimes(2);
});


it("background prepared-reference invalidation cannot dismiss a fresh start failure", async () => {
  vi.mocked(client.getJSON).mockResolvedValue({ available: true, prepared: false, filename: "questions.pdf" });
  startMutateAsync.mockRejectedValue(new APIError(422, "provider_request_rejected", { detail: { code: "provider_request_rejected" } }));
  const user = userEvent.setup();
  renderPage();
  await uploadProblemFile(user);
  preflightMutateAsync.mockResolvedValue({ source_token: "prepared", source: { stored_file_id: "stored" } });
  await user.click(screen.getByRole("button", { name: "识别并准备题目资料" }));
  await waitFor(() => expect(startMutateAsync).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "重新识别全部资料" })).toBeInTheDocument());
  expect(screen.getByRole("link", { name: "返回修改配置" })).toHaveAttribute("href", "/tasks/task-1/upload/problems");
});

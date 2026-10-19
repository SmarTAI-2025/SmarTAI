import "fake-indexeddb/auto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { APIError, getJSON, putJSON, postMultipart, deleteJSON } from "@/api/client";
import { gradingSetupKeys, taskKeys } from "@/api/hooks/keys";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { PageDraftSession } from "@/hooks/useDraftProtection";
import { clearPageDrafts } from "@/lib/pageDraftStore";
import type { GradingSetup, GradingSetupResponse } from "@/types";
import { GradingSetupPage } from "./GradingSetupPage";

vi.mock("@/api/client", async (original) => ({
  ...await original<typeof import("@/api/client")>(),
  getJSON: vi.fn(), putJSON: vi.fn(), postMultipart: vi.fn(), deleteJSON: vi.fn(),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }) }));

const defaults: GradingSetup = {
  schema_version: 1, selected_provider_ids: ["provider-1"], primary_provider_id: "provider-1",
  aggregation_method: "single", expert_weights: {}, multi_sample_n: 1, knowledge_scope: "none",
  strictness: 50, allow_partial_credit: true, feedback_tone: "neutral", feedback_length: "medium",
  feedback_language: "zh", suggest_corrections: true, low_confidence_threshold: 0.6, teacher_notes: "",
};
const document = { doc_id: "doc-1", filename: "Calculus notes.pdf", library_material_id: "material-1", chunk_count: 4, uploaded_at: 1 };
let docs: typeof document[];
let saved: GradingSetup | null;
let revision: number;
let model: string;
let enabled: boolean;
let otherModel: string | null;
let defaultProvider: string;
let refreshGate: Promise<void> | undefined;
const setupUrl = "/tasks/task-1/grading-setup";
const response = (): GradingSetupResponse => ({
  task_id: "task-1", task_status: "submissions_ready", workflow_revision: revision,
  configured: saved !== null, grading_setup: saved,
  // Match the backend: suggestions change with attachments before the first save.
  suggested_setup: { ...defaults, selected_provider_ids: [defaultProvider], primary_provider_id: defaultProvider, knowledge_scope: docs.length ? "all_task_docs" : "none" },
  grading_setup_fingerprint: saved ? JSON.stringify(saved) : null, grading_setup_updated_at: saved ? 1 : null,
  available_experts: [{ provider_id: "provider-1", provider_type: "gemini", model, display_name: "Grader", enabled, scope: "owner", is_shared: false, editable: true, max_concurrent: 1, rpm: 10 }, ...(otherModel ? [{ provider_id: "provider-2", provider_type: "gemini" as const, model: otherModel, display_name: "Other model", enabled: true, scope: "owner" as const, is_shared: false, editable: true, max_concurrent: 1, rpm: 10 }] : [])],
  knowledge: { scope_options: ["none", "all_task_docs"], task_doc_count: docs.length, task_docs: docs },
  readiness: { ready: true, blocking_issues: [], warnings: [] },
});

beforeEach(async () => {
  vi.clearAllMocks();
  await clearPageDrafts();
  docs = []; saved = null; revision = 7; model = "example-model"; enabled = true; otherModel = null; refreshGate = undefined;
  defaultProvider = "provider-1";
  vi.mocked(getJSON).mockImplementation(async (url) => {
    if (url === setupUrl) return structuredClone(response()) as never;
    if (url === "/tasks/task-1/kb") return { docs: structuredClone(docs) } as never;
    if (url === "/course-materials/") return { items: [{ material_id: "material-1", filename: document.filename, category: "lecture" }] } as never;
    if (url === "/tasks/task-1") { await refreshGate; return { task_id: "task-1" } as never; }
    throw new Error(`Unexpected GET ${url}`);
  });
  vi.mocked(postMultipart).mockImplementation(async () => { docs = [document]; revision++; return { ...document, workflow_revision: revision } as never; });
  vi.mocked(deleteJSON).mockImplementation(async () => { docs = []; revision++; return { status: "deleted", workflow_revision: revision } as never; });
  vi.mocked(putJSON).mockImplementation(async (url, body) => {
    expect(url).toBe(setupUrl);
    const input = body as { expected_workflow_revision: number; grading_setup: GradingSetup };
    expect(input.expected_workflow_revision).toBe(revision);
    saved = structuredClone(input.grading_setup); revision++;
    return structuredClone(response()) as never;
  });
});

function TaskObserver() {
  useQuery({ queryKey: taskKeys.detail("task-1"), queryFn: () => getJSON("/tasks/task-1") });
  return null;
}
function openEditor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  const router = createMemoryRouter([
    { path: "/tasks/:taskId/grading-setup", element: <PageDraftSession ownerId="conflict-teacher"><DraftLeaveProvider><TaskObserver /><GradingSetupPage /><DraftActions /></DraftLeaveProvider></PageDraftSession> },
    { path: "/tasks/task-1/grading/preflight", element: <div>Grading summary</div> },
  ], { initialEntries: [setupUrl] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { client, router, user: userEvent.setup() };
}

it.each([
  [false, "attach"], [false, "remove"], [true, "attach"], [true, "remove"],
] as const)("drafts and submits edits after a material %s/%s", async (configured, action) => {
  if (action === "remove") docs = [document];
  if (configured) saved = { ...defaults, knowledge_scope: docs.length ? "all_task_docs" : "none" };
  const { user } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  if (action === "attach") {
    await user.click(screen.getByRole("searchbox", { name: "搜索课程资料库" }));
    await user.click(screen.getByRole("button", { name: "选择" }));
    await screen.findByText(/已将.*加入本任务/);
  } else {
    await user.click(await screen.findByRole("button", { name: /移除/ }));
    await screen.findByText("已选择 0/20 份");
  }
  expect(screen.queryAllByText(/任务或模型配置已更新|任务内容已更新/)).toHaveLength(0);
  expect(slider).toHaveValue("70");
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/已暂存 ·/);
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(saved).toMatchObject({ strictness: 70, knowledge_scope: action === "attach" ? "all_task_docs" : "none" });
  expect(putJSON).toHaveBeenCalledTimes(1);
});

it.each([false, true])("does not report its own save as a conflict (configured=%s), including a return after answer review", async (configured) => {
  if (configured) saved = { ...defaults };
  const { user, router, client } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "60" } });
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/已暂存 ·/);
  fireEvent.change(slider, { target: { value: "70" } });
  let release!: () => void;
  refreshGate = new Promise(resolve => { release = resolve; });
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await waitFor(() => expect(client.getQueryData<GradingSetupResponse>(gradingSetupKeys.detail("task-1"))?.grading_setup?.strictness).toBe(70));
  try {
    expect(screen.queryAllByText(/任务或模型配置已更新|任务内容已更新/)).toHaveLength(0);
  } finally { await act(async () => { release(); }); }
  await screen.findByText("Grading summary");
  // Reviewing an answer changes the task revision, not grading settings.
  revision++;
  await act(async () => {
    await client.invalidateQueries({ queryKey: gradingSetupKeys.detail("task-1") });
    await router.navigate(setupUrl);
  });
  expect(await screen.findByRole("slider")).toHaveValue("70");
  expect(screen.queryAllByText(/任务或模型配置已更新|任务内容已更新/)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(putJSON).toHaveBeenCalledTimes(2);
});

it("still protects a concurrent task setting change until the teacher resolves it", async () => {
  saved = { ...defaults };
  const { user, client } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  saved = { ...defaults, strictness: 20 };
  revision++;
  await act(async () => { await client.invalidateQueries({ queryKey: gradingSetupKeys.detail("task-1") }); });
  await screen.findByText(/任务内容已更新，当前输入已保留/);
  expect(slider).toHaveValue("70");
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  expect(putJSON).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/请先核对服务器变化/);
  await user.click(screen.getByRole("button", { name: "保留当前输入" }));
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(saved?.strictness).toBe(70);
});

it.each(["add", "change", "remove"])("ignores an unselected model %s while preserving drafts and submission", async action => {
  saved = { ...defaults };
  if (action !== "add") otherModel = "unused";
  const { user, client } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  otherModel = action === "remove" ? null : "unused-changed";
  await act(async () => { await client.invalidateQueries({ queryKey: gradingSetupKeys.detail("task-1") }); });
  expect(screen.queryAllByText(/任务或模型配置已更新|任务内容已更新/)).toHaveLength(0);
  expect(slider).toHaveValue("70");
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/已暂存 ·/);
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(saved?.selected_provider_ids).toEqual(["provider-1"]);
});

it("blocks an unavailable selected model with validation, without a false draft conflict", async () => {
  saved = { ...defaults };
  const { user, client } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  enabled = false;
  await act(async () => { await client.invalidateQueries({ queryKey: gradingSetupKeys.detail("task-1") }); });
  expect(screen.queryAllByText(/任务内容已更新，当前输入已保留/)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/已暂存 ·/);
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  expect(putJSON).not.toHaveBeenCalled();
  expect(slider).toHaveValue("70");
  expect(screen.queryByText("Grading summary")).not.toBeInTheDocument();
});

it("preserves the first unsaved configuration when another model becomes the default", async () => {
  const { user, client } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  otherModel = "new-default"; defaultProvider = "provider-2";
  await act(async () => { await client.invalidateQueries({ queryKey: gradingSetupKeys.detail("task-1") }); });
  expect(screen.queryAllByText(/任务或模型配置已更新|任务内容已更新/)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "暂存" }));
  await screen.findByText(/已暂存 ·/);
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(saved).toMatchObject({ strictness: 70, selected_provider_ids: ["provider-1"] });
});

it("releases the save guard after a rejected stale write and still requires conflict resolution", async () => {
  saved = { ...defaults };
  const { user } = openEditor();
  const slider = await screen.findByRole("slider");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(slider, { target: { value: "70" } });
  vi.mocked(putJSON).mockImplementationOnce(async () => {
    saved = { ...defaults, strictness: 20 }; revision++;
    throw new APIError(409, "workflow_revision_conflict", { detail: { code: "workflow_revision_conflict" } });
  });
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText(/任务内容已更新，当前输入已保留/);
  expect(slider).toHaveValue("70");
  expect(slider).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  expect(putJSON).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "保留当前输入" }));
  await user.click(screen.getByRole("button", { name: "保存并查看批改摘要" }));
  await screen.findByText("Grading summary");
  expect(saved?.strictness).toBe(70);
  expect(putJSON).toHaveBeenCalledTimes(2);
});

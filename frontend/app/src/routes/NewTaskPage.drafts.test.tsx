import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, Outlet } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { clearPageDrafts } from "@/lib/pageDraftStore";
import * as store from "@/lib/pageDraftStore";
import { toast } from "sonner";
import { NewTaskPage } from "./NewTaskPage";
import { FrontierContext } from "@/lib/frontierContext";

const state = vi.hoisted(() => ({
  task: { task_id: "t1", name: "Saved task", semester_id: "2026-fall", course_id: "c1", tag_ids: ["tag1"] },
  create: vi.fn(), update: vi.fn(),
  locale: "zh-CN",
}));
vi.mock("@/api/hooks", () => ({
  useTask: (id?: string) => ({ data: id ? state.task : undefined, isLoading: false, isSuccess: true }),
  useCourses: () => ({ data: [{ id: "c1", name: "Physics" }], isSuccess: true }),
  useTags: () => ({ data: [{ id: "tag1", name: "Weekly" }], isSuccess: true }),
  useCourseSearch: () => ({ data: { items: [] } }),
  useTagSearch: () => ({ data: { items: [] } }),
  useCreateCourse: () => ({}), useCreateTag: () => ({}),
  useCreateTask: () => ({ mutateAsync: state.create }),
  useUpdateTask: () => ({ mutateAsync: state.update }),
  useExperts: () => ({ data: [] }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: state.locale, t: (key: string) => key }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), warning: vi.fn() } }));

function mount(path = "/tasks/new", frontier = false) {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [
    { path: "/tasks/new", element: <NewTaskPage /> },
    { path: "/tasks/:taskId/edit", element: <NewTaskPage /> },
    { path: "/tasks/:taskId/upload/problems", element: <div>Upload problems</div> },
    { path: "/settings/byok", element: <div>BYOK</div> },
  ] }], { initialEntries: [path] });
  return { ...render(<FrontierContext.Provider value={frontier}><PageDraftSession ownerId="metadata-teacher"><RouterProvider router={router} /></PageDraftSession></FrontierContext.Provider>), router };
}
afterEach(() => vi.restoreAllMocks());
const name = () => screen.getByLabelText("newTaskNameLabel");
beforeEach(async () => {
  vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File);
  await clearPageDrafts();
  state.task.name = "Saved task";
  state.locale = "zh-CN";
  state.create.mockReset().mockResolvedValue({ task_id: "created" });
  state.update.mockReset().mockResolvedValue({});
});

it("restores metadata through BYOK/back/forward and clears it after successful creation", async () => {
  const { router } = mount();
  fireEvent.change(name(), { target: { value: "Unsubmitted assignment" } });
  fireEvent.click(screen.getByRole("link", { name: "newTaskManageModels" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" }));
  await screen.findByText("BYOK");
  await act(() => router.navigate(-1));
  await waitFor(() => expect(name()).toHaveValue("Unsubmitted assignment"));
  await act(() => router.navigate(1));
  await act(() => router.navigate(-1));
  await waitFor(() => expect(name()).toHaveValue("Unsubmitted assignment"));
  expect(state.create).not.toHaveBeenCalled();
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  await act(() => router.navigate("/tasks/new"));
  expect(name()).toHaveValue("");
  expect(state.create).toHaveBeenCalledTimes(1);
});

it("waits for draft cleanup before navigating and cannot resubmit during cleanup", async () => {
  mount();
  fireEvent.change(name(), { target: { value: "Complete once" } });
  await saveDraft();
  const remove = store.removePageDraft;
  let release!: () => void;
  vi.spyOn(store, "removePageDraft").mockImplementationOnce(async (...args) => {
    await new Promise<void>(resolve => { release = resolve; });
    await remove(...args);
  });
  fireEvent.submit(document.querySelector("form")!);
  await waitFor(() => expect(release).toBeTypeOf("function"));
  expect(screen.queryByText("Upload problems")).not.toBeInTheDocument();
  fireEvent.submit(document.querySelector("form")!);
  expect(state.create).toHaveBeenCalledTimes(1);
  await act(async () => release());
  await screen.findByText("Upload problems");
  expect(await store.listPageDrafts("metadata-teacher")).toEqual([]);
});

it("keeps formal success and a visible warning when local cleanup fails", async () => {
  mount();
  fireEvent.change(name(), { target: { value: "Already created" } });
  await saveDraft();
  vi.spyOn(store, "removePageDraft").mockRejectedValueOnce(new Error("storage unavailable"));
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  expect(state.create).toHaveBeenCalledTimes(1);
  expect(toast.warning).toHaveBeenCalledWith(expect.stringContaining("无需重复提交"));
});

it("stays with inputs on explicit storage failure and restores server state on reset", async () => {
  const { router } = mount("/tasks/t1/edit");
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
  fireEvent.change(name(), { target: { value: "Private edit" } });
  const put = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
  await act(() => router.navigate("/settings/byok")); fireEvent.click(screen.getByRole("button", { name: "暂存并离开" }));
  await screen.findAllByText(/存储空间不足/); expect(name()).toHaveValue("Private edit"); expect(router.state.location.pathname).toBe("/tasks/t1/edit");
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" })); put.mockRestore();
  vi.spyOn(window, "confirm").mockReturnValue(true); fireEvent.click(screen.getByRole("button", { name: "删除本页草稿" }));
  await waitFor(() => expect(name()).toHaveValue("Saved task")); expect(state.update).not.toHaveBeenCalled();
});

it("uses the newly saved server state instead of a draft for an older metadata snapshot", async () => {
  const { router } = mount("/tasks/t1/edit");
  fireEvent.change(name(), { target: { value: "Old local edit" } });
  await saveDraft();
  await act(() => router.navigate("/settings/byok"));
  state.task = { ...state.task, name: "New server title" };
  await act(() => router.navigate(-1));
  await waitFor(() => expect(name()).toHaveValue("New server title"));
  expect(await screen.findByText(/服务器内容或当前输入已变化/)).toBeInTheDocument();
  expect(state.update).not.toHaveBeenCalled();
});

it("reuses the create idempotency key after an uncertain response and navigation", async () => {
  state.create.mockRejectedValueOnce(new Error("connection lost"));
  const { router } = mount();
  fireEvent.change(name(), { target: { value: "Retry assignment" } });
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByRole("alert");
  await saveDraft();
  await act(() => router.navigate("/settings/byok"));
  await act(() => router.navigate(-1));
  await waitFor(() => expect(name()).toHaveValue("Retry assignment"));
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  expect(state.create.mock.calls[1][0]).toEqual(state.create.mock.calls[0][0]);
});

it("reuses the request key when retried as soon as the saved form becomes visible", async () => {
  state.create.mockRejectedValueOnce(new Error("connection lost"));
  const { router } = mount();
  fireEvent.change(name(), { target: { value: "Immediate retry" } });
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByRole("alert");
  await saveDraft();
  await act(() => router.navigate("/settings/byok"));
  // Retry in the DOM-observer microtask: the restored form is visible, but
  // React's passive effects may not yet have synchronized the submission key.
  const observer = new MutationObserver(() => {
    const input = document.querySelector<HTMLInputElement>("#new-task-name");
    if (input?.value !== "Immediate retry") return;
    observer.disconnect();
    input.form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true });
  try {
    await act(() => router.navigate(-1));
    await screen.findByText("Upload problems");
    expect(state.create).toHaveBeenCalledTimes(2);
    expect(state.create.mock.calls[1][0]).toEqual(state.create.mock.calls[0][0]);
  } finally { observer.disconnect(); }
});

it.each(["changed input", "deleted draft"])("uses a new request key after %s", async (change) => {
  state.create.mockRejectedValueOnce(new Error("connection lost"));
  mount();
  fireEvent.change(name(), { target: { value: "First attempt" } });
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByRole("alert");
  if (change === "deleted draft") {
    await saveDraft();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "删除本页草稿" }));
    await waitFor(() => expect(name()).toHaveValue(""));
    fireEvent.change(name(), { target: { value: "First attempt" } });
  } else {
    fireEvent.change(name(), { target: { value: "Different assignment" } });
  }
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  expect(state.create).toHaveBeenCalledTimes(2);
  expect(state.create.mock.calls[1][0].idempotencyKey).not.toBe(state.create.mock.calls[0][0].idempotencyKey);
});

async function saveDraft() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(screen.getByText(/已暂存 ·/)).toBeInTheDocument()); }

it.each(["zh-CN", "en-US"])("keeps Demo presets immutable without restoring a local metadata draft (%s)", async (locale) => {
  const original = mount("/tasks/t1/edit");
  fireEvent.change(name(), { target: { value: "Old browser draft" } });
  await saveDraft();
  original.unmount();
  state.locale = locale;
  mount("/tasks/t1/edit", true);
  await waitFor(() => expect(name()).toHaveValue("Saved task"));
  expect(name()).toBeDisabled();
  expect(screen.queryByRole("region", { name: "本地草稿" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "进入上传题目" : "Continue to Questions" }));
  await screen.findByText("Upload problems");
  expect(state.update).not.toHaveBeenCalled();
  expect(state.create).not.toHaveBeenCalled();
});

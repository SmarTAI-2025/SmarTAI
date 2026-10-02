import { act, fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { clearPageDrafts } from "@/lib/pageDraftStore";
import { NewTaskPage } from "./NewTaskPage";

const state = vi.hoisted(() => ({
  task: { task_id: "t1", name: "Saved task", semester_id: "2026-fall", course_id: "c1", tag_ids: ["tag1"] },
  create: vi.fn(), update: vi.fn(),
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
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));

function mount(path = "/tasks/new") {
  const router = createMemoryRouter([
    { path: "/tasks/new", element: <NewTaskPage /> },
    { path: "/tasks/:taskId/edit", element: <NewTaskPage /> },
    { path: "/tasks/:taskId/upload/problems", element: <div>Upload problems</div> },
    { path: "/settings/byok", element: <div>BYOK</div> },
  ], { initialEntries: [path] });
  return { ...render(<PageDraftSession ownerId="metadata-teacher"><RouterProvider router={router} /></PageDraftSession>), router };
}
const name = () => screen.getByLabelText("newTaskNameLabel");
beforeEach(() => {
  clearPageDrafts();
  state.task.name = "Saved task";
  state.create.mockReset().mockResolvedValue({ task_id: "created" });
  state.update.mockReset().mockResolvedValue({});
});

it("restores metadata through BYOK/back/forward and clears it after successful creation", async () => {
  const { router } = mount();
  fireEvent.change(name(), { target: { value: "Unsubmitted assignment" } });
  fireEvent.click(screen.getByRole("link", { name: "newTaskManageModels" }));
  await screen.findByText("BYOK");
  await act(() => router.navigate(-1));
  expect(name()).toHaveValue("Unsubmitted assignment");
  await act(() => router.navigate(1));
  await act(() => router.navigate(-1));
  expect(state.create).not.toHaveBeenCalled();
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  await act(() => router.navigate("/tasks/new"));
  expect(name()).toHaveValue("");
  expect(state.create).toHaveBeenCalledTimes(1);
});

it("preserves an edited task even when browser storage is blocked; discard restores saved metadata", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("blocked", "QuotaExceededError"); });
  const { router } = mount("/tasks/t1/edit");
  expect(name()).toHaveValue("Saved task");
  expect(screen.getByText("Physics")).toBeInTheDocument();
  expect(screen.getByText("Weekly")).toBeInTheDocument();
  fireEvent.change(name(), { target: { value: "Private edit" } });
  await act(() => router.navigate("/settings/byok"));
  await act(() => router.navigate(-1));
  expect(name()).toHaveValue("Private edit");
  expect(screen.getByText(/浏览器暂存不可用/)).toBeInTheDocument();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "放弃草稿" }));
  expect(name()).toHaveValue("Saved task");
  expect(state.update).not.toHaveBeenCalled();
});

it("uses the newly saved server state instead of a draft for an older metadata snapshot", async () => {
  const { router } = mount("/tasks/t1/edit");
  fireEvent.change(name(), { target: { value: "Old local edit" } });
  await act(() => router.navigate("/settings/byok"));
  state.task = { ...state.task, name: "New server title" };
  await act(() => router.navigate(-1));
  expect(name()).toHaveValue("New server title");
  expect(state.update).not.toHaveBeenCalled();
});

it("reuses the create idempotency key after an uncertain response and navigation", async () => {
  state.create.mockRejectedValueOnce(new Error("connection lost"));
  const { router } = mount();
  fireEvent.change(name(), { target: { value: "Retry assignment" } });
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByRole("alert");
  await act(() => router.navigate("/settings/byok"));
  await act(() => router.navigate(-1));
  fireEvent.submit(document.querySelector("form")!);
  await screen.findByText("Upload problems");
  expect(state.create.mock.calls[0][0].idempotencyKey).toBe(state.create.mock.calls[1][0].idempotencyKey);
});

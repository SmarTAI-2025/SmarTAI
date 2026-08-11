import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { NewTaskPage } from "./NewTaskPage";

const { useCourses, useTags } = vi.hoisted(() => ({
  useCourses: vi.fn(() => ({ data: [], isSuccess: false, isError: false })),
  useTags: vi.fn(() => ({ data: [], isSuccess: false, isError: false })),
}));

vi.mock("@/api/hooks", () => ({
  useCurrentUser: () => ({
    data: { id: "frontier_demo_user", username: "frontier-demo", role: "teacher" },
    isSuccess: true,
  }),
  useCourses,
  useCourseSearch: () => ({ data: { items: [] }, isFetching: false }),
  useCreateCourse: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useCreateTag: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useCreateTask: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useExperts: () => ({ data: [], isLoading: false, isError: false }),
  useTask: (taskId?: string) => ({
    data: taskId ? { task_id: taskId, name: "SmarTAI Live Demo · fixed sample" } : undefined,
    isLoading: false,
    isError: false,
  }),
  useTags,
  useTagSearch: () => ({ data: { items: [] }, isFetching: false }),
  useUpdateTask: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));

describe("NewTaskPage Frontier Demo lock", () => {
  it("shows fixed sample metadata and no editable task action", () => {
    const router = createMemoryRouter([
      { path: "/tasks/:taskId/edit", element: <NewTaskPage /> },
    ], { initialEntries: ["/tasks/task-1/edit"] });

    render(<RouterProvider router={router} />);

    expect(screen.getByRole("heading", { name: "示例任务信息" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("SmarTAI Live Demo · fixed sample")).toBeDisabled();
    expect(screen.getByDisplayValue("STEM Reasoning Lab")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /newTaskSaveAndReturn|newTaskCreateAndAdd/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /返回 Demo 继续/ })).toHaveAttribute(
      "href",
      "/frontier/live?taskId=task-1",
    );
    expect(useCourses).toHaveBeenCalledWith({ enabled: false });
    expect(useTags).toHaveBeenCalledWith({ enabled: false });
  });
});

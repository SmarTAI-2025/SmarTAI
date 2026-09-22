import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CourseMaterial } from "@/types";
import { MaterialDialog } from "./CourseLibraryDialogs";

const mocks = vi.hoisted(() => ({
  deleteMaterial: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("@/api/hooks", () => ({
  useCreateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useDeleteCourseMaterial: () => ({ isPending: false, mutateAsync: mocks.deleteMaterial }),
  useDeleteCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterial: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUploadCourseMaterial: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN" }),
}));

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: mocks.toastSuccess },
}));

const material: CourseMaterial = {
  material_id: "material-1",
  course_id: null,
  group_id: null,
  filename: "notes.md",
  category: "lecture",
  labels: [],
  content_type: "text/markdown",
  size_bytes: 5,
  sha256: "a".repeat(64),
  created_at: 1,
  updated_at: 1,
  last_used_at: null,
  task_reference_count: 0,
  parse_status: "ready",
  group_name: null,
  course_name: null,
  course_code: null,
  match_kind: null,
  match_score: null,
  match_reason: null,
};

describe("course material deletion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.deleteMaterial.mockResolvedValue({
      status: "deletion_pending",
      material_id: material.material_id,
      detached_task_references: 0,
      cleanup_operation_id: "cleanup-1",
    });
  });

  it("reports async deletion truthfully without offering a manual retry", async () => {
    const user = userEvent.setup();
    render(
      <MaterialDialog
        material={material}
        courses={[]}
        groups={[]}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        onDeleted={vi.fn()}
        initialConfirmDelete
      />,
    );

    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mocks.toastSuccess).toHaveBeenCalledWith(
      "资料已移除",
      {
        description: "系统会在后台自动清理原文件；完成前仍计入知识库占用，无需手动重试。",
      },
    ));
    expect(screen.queryByRole("button", { name: /重试|清理/ })).not.toBeInTheDocument();
  });
});

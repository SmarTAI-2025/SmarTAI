import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import type { CourseMaterial } from "@/types";
import { MaterialDialog, UploadDialog } from "./CourseLibraryDialogs";

const mocks = vi.hoisted(() => ({
  deleteMaterial: vi.fn(),
  toastSuccess: vi.fn(),
  upload: vi.fn(),
}));

vi.mock("@/api/hooks", () => ({
  useCreateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useDeleteCourseMaterial: () => ({ isPending: false, mutateAsync: mocks.deleteMaterial }),
  useDeleteCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterial: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUploadCourseMaterial: () => ({ isPending: false, mutateAsync: mocks.upload }),
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

  it("uploads native-only knowledge only when explicitly selected", async () => {
    const user = userEvent.setup();
    mocks.upload.mockResolvedValue({ ...material, created: true, parse_status: "processing" });
    render(<UploadDialog courses={[]} groups={[]} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const checkbox = screen.getByRole("checkbox", { name: "仅提取已有文字（不调用模型）" });
    expect(checkbox).not.toBeChecked();
    const file = new File(["synthetic PDF"], "book.pdf", { type: "application/pdf" });
    await user.upload(screen.getByLabelText(/选择一份资料文件/), file);
    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: "上传资料" }));
    await waitFor(() => expect(mocks.upload).toHaveBeenCalledWith(expect.objectContaining({ file, nativeOnly: true })));
    expect(mocks.toastSuccess).toHaveBeenCalledWith("资料已保存，已进入处理队列");
  });

  it.each([
    ["empty.txt", "", "文件为空"],
    ["answer.docx", "text", "资料库仅支持"],
    ["large.pdf", "pdf", "文件超过"],
  ])("explains rejected upload %s and recovers with a valid replacement", async (name, content, message) => {
    const user = userEvent.setup();
    render(<UploadDialog courses={[]} groups={[]} onClose={vi.fn()} onUploaded={vi.fn()} />);
    const input = screen.getByLabelText(/选择一份资料文件/);
    const file = new File([content], name);
    if (name === "large.pdf") Object.defineProperty(file, "size", { value: 64 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [file] } });
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "上传资料" })).toBeDisabled();
    expect(mocks.upload).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { files: [new File(["notes"], "valid.txt", { type: "text/plain" })] } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传资料" })).toBeEnabled();
  });

  it("keeps server upload failures visible in the dialog", async () => {
    const user = userEvent.setup();
    mocks.upload.mockRejectedValue(new APIError(422, "unreadable", { code: "invalid_course_material" }));
    render(<UploadDialog courses={[]} groups={[]} onClose={vi.fn()} onUploaded={vi.fn()} />);
    await user.upload(screen.getByLabelText(/选择一份资料文件/), new File(["bad pdf"], "broken.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("button", { name: "上传资料" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("文件内容无法作为资料读取");
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

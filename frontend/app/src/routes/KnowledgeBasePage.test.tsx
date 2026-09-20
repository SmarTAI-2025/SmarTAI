import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import type { KnowledgeStorageUsage } from "@/types";
import { KnowledgeBasePage } from "./KnowledgeBasePage";

const mocks = vi.hoisted(() => ({
  uploadCourseMaterial: vi.fn(),
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

const retryingUsage: KnowledgeStorageUsage = {
  used_bytes: 12 * 1024 * 1024,
  limit_bytes: 100 * 1024 * 1024,
  available_bytes: 88 * 1024 * 1024,
  available_document_bytes: 5 * 1024 * 1024,
  cleanup_pending_bytes: 3 * 1024 * 1024,
  retrying_cleanup_bytes: 2 * 1024 * 1024,
  reserved_bytes: 1024 * 1024,
  cleanup_pending_count: 2,
  retrying_cleanup_count: 1,
  reserved_count: 1,
};

vi.mock("@/api/hooks", () => ({
  useCourseMaterials: () => ({
    data: { items: [], summary: { materials: 0, groups: 0, referenced: 0 } },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useCourseMaterialGroups: () => ({
    data: { items: [] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useCourses: () => ({ data: [] }),
  useKnowledgeStorageUsage: () => ({ data: retryingUsage, isLoading: false }),
  useUploadCourseMaterial: () => ({
    isPending: false,
    mutateAsync: mocks.uploadCourseMaterial,
  }),
  useCreateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useDeleteCourseMaterialGroup: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useUpdateCourseMaterial: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useDeleteCourseMaterial: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/components/knowledge-base/CourseLibraryTable", () => ({
  CourseLibraryTable: () => <div data-testid="course-library-table" />,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

vi.mock("sonner", () => ({
  toast: { error: mocks.toastError, success: mocks.toastSuccess },
}));

describe("KnowledgeBasePage storage quota", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.uploadCourseMaterial.mockResolvedValue({ created: true });
  });

  it("shows independent usage and automatic cleanup retry without a retry action", () => {
    render(<KnowledgeBasePage />);

    expect(screen.getByRole("heading", { name: "知识库独立空间" })).toBeInTheDocument();
    expect(screen.getByText("12.0 MB")).toBeInTheDocument();
    expect(screen.getByText("/ 100.0 MB")).toBeInTheDocument();
    expect(screen.getByText("88.0 MB")).toBeInTheDocument();
    expect(screen.getByText("正常资料占用")).toBeInTheDocument();
    expect(screen.getByText("5.0 MB")).toBeInTheDocument();
    expect(screen.queryByText("当前单份可上传")).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "知识库空间占用" })).toHaveAttribute("aria-valuenow", "12");
    expect(screen.getByText("刚删除失败，系统正在后台自动重试；完成前仍计入占用，无需手动操作。")).toBeInTheDocument();
    expect(screen.getByText(/保存到个人或课程资料库的内容会长期保留，不会随批改任务删除/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /清理|重试/ })).not.toBeInTheDocument();
  });

  it("maps the stable 413 quota code to a friendly upload message", async () => {
    mocks.uploadCourseMaterial.mockRejectedValue(new APIError(
      413,
      "knowledge_storage_quota_exceeded",
      { error: { code: "knowledge_storage_quota_exceeded" } },
    ));
    const user = userEvent.setup();
    render(<KnowledgeBasePage />);

    await user.click(screen.getByRole("button", { name: "上传资料" }));
    const dialog = screen.getByRole("dialog", { name: "上传资料" });
    await user.upload(
      within(dialog).getByLabelText(/选择一份资料文件/),
      new File(["chapter"], "chapter.md", { type: "text/markdown" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "上传资料" }));

    await waitFor(() => expect(mocks.toastError).toHaveBeenCalledWith(
      "资料上传失败",
      {
        description: "知识库空间已满或剩余空间不足。请删除不再需要的资料；若系统正在后台清理，请等待自动完成后再上传。",
      },
    ));
    expect(mocks.toastError).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ description: "knowledge_storage_quota_exceeded" }),
    );
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { KnowledgeSection } from "./GradingSetupPage";

const attachMaterial = vi.fn();

vi.mock("@/api/hooks", () => ({
  useCourseMaterials: vi.fn(),
  useDeleteKBDoc: vi.fn(),
  useGradingSetup: vi.fn(),
  useKBDocs: vi.fn(),
  useSaveGradingSetup: vi.fn(),
  useUploadKBDoc: vi.fn(),
}));

const {
  useCourseMaterials,
  useDeleteKBDoc,
  useKBDocs,
  useUploadKBDoc,
} = await import("@/api/hooks");

describe("GradingSetupPage task-material picker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    attachMaterial.mockResolvedValue({
      status: "already_done",
      task_id: "task-1",
      doc_id: "doc-1",
      filename: "Calculus notes.pdf",
      chunk_count: 4,
      workflow_revision: 2,
    });
    (useKBDocs as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { docs: [] },
      isLoading: false,
      isSuccess: true,
    });
    (useCourseMaterials as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        items: [{
          material_id: "material-1",
          filename: "Calculus notes.pdf",
          course_name: "Calculus I",
          category: "lecture",
        }],
      },
      isLoading: false,
    });
    (useUploadKBDoc as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      isPending: false,
      mutateAsync: attachMaterial,
    });
    (useDeleteKBDoc as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn(),
    });
  });

  it("keeps the picker interactive and confirms a library selection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    render(
      <MemoryRouter>
        <KnowledgeSection
          locale="zh-CN"
          taskId="task-1"
          value="none"
          onChange={onChange}
        />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("searchbox", { name: "搜索课程资料库" }));
    await user.click(screen.getByRole("button", { name: "选择" }));

    expect(attachMaterial).toHaveBeenCalledWith({
      taskId: "task-1",
      libraryMaterialId: "material-1",
    });
    expect(onChange).toHaveBeenCalledWith("all_task_docs");
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "已将“Calculus notes.pdf”加入本任务。",
      );
    });
  });

  it("shows friendly knowledge quota copy for a task-only upload", async () => {
    attachMaterial.mockRejectedValue(new APIError(
      413,
      "knowledge_storage_quota_exceeded",
      { error: { code: "knowledge_storage_quota_exceeded" } },
    ));
    const user = userEvent.setup();

    const { container } = render(
      <MemoryRouter>
        <KnowledgeSection
          locale="zh-CN"
          taskId="task-1"
          value="none"
          onChange={vi.fn()}
        />
      </MemoryRouter>,
    );

    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    await user.upload(
      input as HTMLInputElement,
      new File(["notes"], "notes.md", { type: "text/markdown" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("知识库空间已满或剩余空间不足");
    });
    expect(screen.getByRole("alert")).not.toHaveTextContent("knowledge_storage_quota_exceeded");
  });
});

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SourceFileDescriptor, TaskSourceFiles } from "@/types/sourcePreview";
import { useSourcePreview } from "./useSourcePreview";

const apiMocks = vi.hoisted(() => ({
  getTaskSourceFiles: vi.fn(),
  loadSourcePreviewFile: vi.fn(),
}));

vi.mock("@/api/sourcePreview", () => ({
  getTaskSourceFiles: apiMocks.getTaskSourceFiles,
  loadSourcePreviewFile: apiMocks.loadSourcePreviewFile,
  sourcePreviewErrorCode: (error: unknown) => (
    typeof error === "object" && error && "code" in error
      ? String((error as { code: unknown }).code)
      : "source_preview_load_failed"
  ),
}));

const sourceOne: SourceFileDescriptor = {
  source_id: "source-1",
  file_id: "file-1",
  display_name: "S001.pdf",
  mime_type: "application/pdf",
  size_bytes: 41,
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};
const sourceTwo: SourceFileDescriptor = {
  ...sourceOne,
  source_id: "source-2",
  file_id: "file-2",
  display_name: "S002.pdf",
};
const catalog: TaskSourceFiles = {
  task_id: "task-1",
  workflow_revision: 9,
  problem_source: null,
  submission_sources: {
    "source-1": sourceOne,
    "source-2": sourceTwo,
  },
};

beforeEach(() => {
  apiMocks.getTaskSourceFiles.mockReset().mockResolvedValue(catalog);
  apiMocks.loadSourcePreviewFile.mockReset().mockImplementation(async (_taskId, descriptor) => (
    new Blob([descriptor.file_id], { type: "application/pdf" })
  ));
  let nextUrl = 0;
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => `blob:source-${++nextUrl}`),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
});

describe("useSourcePreview", () => {
  it("maps students only by source_id and revokes URLs on switch and close", async () => {
    let resolveSecond: ((blob: Blob) => void) | null = null;
    apiMocks.loadSourcePreviewFile.mockImplementation(async (_taskId, descriptor) => {
      if (descriptor.file_id === "file-2") {
        return new Promise<Blob>((resolve) => {
          resolveSecond = resolve;
        });
      }
      return new Blob([descriptor.file_id], { type: "application/pdf" });
    });
    const { result, rerender } = renderHook(
      ({ sourceId }) => useSourcePreview({
        taskId: "task-1",
        sourceKind: "submission",
        sourceId,
        displayName: "same-name.pdf",
      }),
      { initialProps: { sourceId: "source-1" } },
    );

    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-1"));
    act(() => result.current.openPreview());
    await waitFor(() => expect(result.current.previewUrl).toBe("blob:source-1"));
    expect(apiMocks.loadSourcePreviewFile).toHaveBeenLastCalledWith("task-1", sourceOne);

    rerender({ sourceId: "source-2" });
    expect(result.current.previewUrl).toBeNull();
    expect(result.current.loadState).toBe("loading");
    act(() => resolveSecond?.(new Blob(["file-2"], { type: "application/pdf" })));
    await waitFor(() => expect(result.current.previewUrl).toBe("blob:source-2"));
    expect(apiMocks.loadSourcePreviewFile).toHaveBeenLastCalledWith("task-1", sourceTwo);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:source-1");

    act(() => result.current.closePreview());
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:source-2"));
  });

  it("revokes the active object URL when the page unmounts", async () => {
    const { result, unmount } = renderHook(() => useSourcePreview({
      taskId: "task-1",
      sourceKind: "submission",
      sourceId: "source-1",
    }));

    await waitFor(() => expect(result.current.triggerState).toBe("ready"));
    act(() => result.current.openPreview());
    await waitFor(() => expect(result.current.previewUrl).toBe("blob:source-1"));
    unmount();

    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:source-1");
  });

  it("never combines a new task id with the previous task descriptor", async () => {
    const taskTwoCatalog: TaskSourceFiles = {
      ...catalog,
      task_id: "task-2",
      submission_sources: { "source-2": sourceTwo },
    };
    apiMocks.getTaskSourceFiles.mockImplementation(async (taskId) => (
      taskId === "task-1" ? catalog : taskTwoCatalog
    ));
    const { result, rerender } = renderHook(
      ({ taskId, sourceId }) => useSourcePreview({
        taskId,
        sourceKind: "submission",
        sourceId,
      }),
      { initialProps: { taskId: "task-1", sourceId: "source-1" } },
    );

    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-1"));
    act(() => result.current.openPreview());
    await waitFor(() => expect(result.current.previewUrl).toBe("blob:source-1"));

    rerender({ taskId: "task-2", sourceId: "source-2" });
    await waitFor(() => expect(result.current.previewUrl).toBe("blob:source-2"));

    expect(apiMocks.loadSourcePreviewFile).not.toHaveBeenCalledWith("task-2", sourceOne);
    expect(apiMocks.loadSourcePreviewFile).toHaveBeenCalledWith("task-2", sourceTwo);
  });
});

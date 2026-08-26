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
  isSourcePreviewCatalogMismatch: (error: unknown) => (
    typeof error === "object" && error !== null && "reason" in error
      && (error as { reason: unknown }).reason === "catalog_scope_mismatch"
  ),
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
const catalogMismatch = {
  code: "source_preview_load_failed",
  reason: "catalog_scope_mismatch",
};
const taskIdCatalogMismatch = {
  ...catalogMismatch,
  mismatch: "task_id",
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
        workflowRevision: 9,
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
      workflowRevision: 9,
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
        workflowRevision: 9,
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

  it("ignores a stale catalog response after the page moves from revision N to N+1", async () => {
    let resolveRevisionNine: ((value: TaskSourceFiles) => void) | null = null;
    const revisionTenCatalog: TaskSourceFiles = {
      ...catalog,
      workflow_revision: 10,
      submission_sources: { "source-2": sourceTwo },
    };
    apiMocks.getTaskSourceFiles.mockImplementation(async (_taskId, expectedWorkflowRevision) => {
      if (expectedWorkflowRevision === 9) {
        return new Promise<TaskSourceFiles>((resolve) => {
          resolveRevisionNine = resolve;
        });
      }
      return revisionTenCatalog;
    });
    const { result, rerender } = renderHook(
      ({ workflowRevision, sourceId }) => useSourcePreview({
        taskId: "task-1",
        workflowRevision,
        sourceKind: "submission",
        sourceId,
      }),
      { initialProps: { workflowRevision: 9, sourceId: "source-1" } },
    );

    rerender({ workflowRevision: 10, sourceId: "source-2" });
    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-2"));

    act(() => resolveRevisionNine?.(catalog));
    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-2"));
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledWith("task-1", 9);
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledWith("task-1", 10);
  });

  it("refreshes the task on a revision jump and adopts only the new catalog scope", async () => {
    let finishRefresh: (() => void) | null = null;
    const refreshTask = vi.fn(() => new Promise<void>((resolve) => {
      finishRefresh = resolve;
    }));
    const revisionTenCatalog: TaskSourceFiles = {
      ...catalog,
      workflow_revision: 10,
      submission_sources: { "source-2": sourceTwo },
    };
    apiMocks.getTaskSourceFiles.mockImplementation(async (_taskId, expectedWorkflowRevision) => {
      if (expectedWorkflowRevision === 9) throw catalogMismatch;
      return revisionTenCatalog;
    });
    const { result, rerender } = renderHook(
      ({ workflowRevision, sourceId }) => useSourcePreview({
        taskId: "task-1",
        workflowRevision,
        sourceKind: "submission",
        sourceId,
        refreshTask,
      }),
      { initialProps: { workflowRevision: 9, sourceId: "source-1" } },
    );

    await waitFor(() => expect(refreshTask).toHaveBeenCalledTimes(1));
    expect(result.current.descriptor).toBeNull();
    rerender({ workflowRevision: 10, sourceId: "source-2" });
    act(() => finishRefresh?.());

    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-2"));
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledWith("task-1", 9);
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledWith("task-1", 10);
  });

  it("bounds a persistent same-scope mismatch to one task refresh and one catalog retry", async () => {
    const refreshTask = vi.fn().mockResolvedValue(undefined);
    apiMocks.getTaskSourceFiles.mockRejectedValue(catalogMismatch);
    const { result } = renderHook(() => useSourcePreview({
      taskId: "task-1",
      workflowRevision: 9,
      sourceKind: "submission",
      sourceId: "source-1",
      refreshTask,
    }));

    await waitFor(() => expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledTimes(2));
    expect(refreshTask).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.triggerState).toBe("unavailable"));
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledTimes(2);
  });

  it("recovers once when a delayed old response has a mismatched task id", async () => {
    let rejectOldResponse: ((reason: unknown) => void) | null = null;
    const refreshTask = vi.fn().mockResolvedValue(undefined);
    apiMocks.getTaskSourceFiles
      .mockImplementationOnce(() => new Promise<TaskSourceFiles>((_resolve, reject) => {
        rejectOldResponse = reject;
      }))
      .mockResolvedValueOnce(catalog);
    const { result } = renderHook(() => useSourcePreview({
      taskId: "task-1",
      workflowRevision: 9,
      sourceKind: "submission",
      sourceId: "source-1",
      refreshTask,
    }));

    await waitFor(() => expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledTimes(1));
    act(() => rejectOldResponse?.(taskIdCatalogMismatch));

    await waitFor(() => expect(result.current.descriptor?.file_id).toBe("file-1"));
    expect(refreshTask).toHaveBeenCalledTimes(1);
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledTimes(2);
  });

  it("does not chase an always-ahead catalog across successive page revisions", async () => {
    let finishRefresh: (() => void) | null = null;
    const refreshTask = vi.fn(() => new Promise<void>((resolve) => {
      finishRefresh = resolve;
    }));
    apiMocks.getTaskSourceFiles.mockRejectedValue(catalogMismatch);
    const { result, rerender } = renderHook(
      ({ workflowRevision }) => useSourcePreview({
        taskId: "task-1",
        workflowRevision,
        sourceKind: "submission",
        sourceId: "source-1",
        refreshTask,
      }),
      { initialProps: { workflowRevision: 9 } },
    );

    await waitFor(() => expect(refreshTask).toHaveBeenCalledTimes(1));
    rerender({ workflowRevision: 10 });
    act(() => finishRefresh?.());

    await waitFor(() => expect(result.current.triggerState).toBe("unavailable"));
    expect(apiMocks.getTaskSourceFiles).toHaveBeenCalledTimes(2);
    expect(apiMocks.getTaskSourceFiles).toHaveBeenNthCalledWith(1, "task-1", 9);
    expect(apiMocks.getTaskSourceFiles).toHaveBeenNthCalledWith(2, "task-1", 10);
    expect(refreshTask).toHaveBeenCalledTimes(1);
  });
});

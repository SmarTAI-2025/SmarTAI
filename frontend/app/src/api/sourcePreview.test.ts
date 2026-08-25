import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import type { SourceFileDescriptor, TaskSourceFiles } from "@/types/sourcePreview";
import {
  getTaskSourceFiles,
  loadSourcePreviewFile,
  sourcePreviewErrorCode,
} from "./sourcePreview";

const clientMocks = vi.hoisted(() => ({
  getBlob: vi.fn(),
  getJSON: vi.fn(),
}));

vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  getBlob: clientMocks.getBlob,
  getJSON: clientMocks.getJSON,
}));

const descriptor: SourceFileDescriptor = {
  source_id: "source-1",
  file_id: "file/1",
  display_name: "题目.pdf",
  mime_type: "application/pdf",
  size_bytes: 3,
  status: "available",
  preview_kind: "pdf",
  unavailable_reason: null,
};

beforeEach(() => {
  clientMocks.getBlob.mockReset();
  clientMocks.getJSON.mockReset();
});

describe("source preview API", () => {
  it("uses the frozen authenticated descriptor and content routes", async () => {
    const catalog: TaskSourceFiles = {
      task_id: "task/1",
      workflow_revision: 7,
      problem_source: descriptor,
      submission_sources: {},
    };
    clientMocks.getJSON.mockResolvedValue(catalog);
    clientMocks.getBlob.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));

    await expect(getTaskSourceFiles("task/1")).resolves.toBe(catalog);
    await expect(loadSourcePreviewFile("task/1", descriptor)).resolves.toBeInstanceOf(Blob);

    expect(clientMocks.getJSON).toHaveBeenCalledWith("/tasks/task%2F1/source-files");
    expect(clientMocks.getBlob).toHaveBeenCalledWith("/tasks/task%2F1/source-files/file%2F1/content");
  });

  it("rejects active or mismatched response MIME types", async () => {
    clientMocks.getBlob.mockResolvedValue(new Blob(["<html>"], { type: "text/html" }));

    await expect(loadSourcePreviewFile("task-1", descriptor)).rejects.toMatchObject({
      code: "source_preview_unsupported_type",
    });
  });

  it("accepts a safe image only when descriptor MIME, kind, and length match", async () => {
    const imageDescriptor: SourceFileDescriptor = {
      ...descriptor,
      file_id: "image-1",
      display_name: "answer.png",
      mime_type: "image/png",
      size_bytes: 3,
      preview_kind: "image",
    };
    clientMocks.getBlob.mockResolvedValue(new Blob(["png"], { type: "image/png" }));

    await expect(loadSourcePreviewFile("task-1", imageDescriptor)).resolves.toBeInstanceOf(Blob);
  });

  it("rejects a catalog for another task and truncated content", async () => {
    clientMocks.getJSON.mockResolvedValue({
      task_id: "task-other",
      workflow_revision: 1,
      problem_source: null,
      submission_sources: {},
    });
    await expect(getTaskSourceFiles("task-1")).rejects.toMatchObject({
      code: "source_preview_load_failed",
    });

    clientMocks.getBlob.mockResolvedValue(new Blob(["shorter"], { type: "application/pdf" }));
    await expect(loadSourcePreviewFile("task-1", descriptor)).rejects.toMatchObject({
      code: "source_preview_load_failed",
    });
  });

  it("maps backend status safely without exposing raw errors", () => {
    expect(sourcePreviewErrorCode(new APIError(404, "private path"))).toBe("source_preview_not_found");
    expect(sourcePreviewErrorCode(new APIError(503, "bucket detail"))).toBe("source_preview_storage_unavailable");
    expect(sourcePreviewErrorCode(new Error("network"))).toBe("source_preview_load_failed");
  });
});

import { APIError, getAPIErrorCode, getBlob, getJSON, normalizeAPIError } from "@/api/client";
import { inferSourcePreviewKind } from "@/lib/sourcePreview";
import type { SourceFileDescriptor, SourcePreviewErrorCode, TaskSourceFiles } from "@/types/sourcePreview";

const SOURCE_PREVIEW_ERROR_CODES = new Set<SourcePreviewErrorCode>([
  "source_preview_not_found",
  "source_preview_processing",
  "source_preview_unavailable",
  "source_preview_unsupported_type",
  "source_preview_storage_unavailable",
  "source_preview_load_failed",
]);

class SourcePreviewResponseError extends Error {
  readonly code: SourcePreviewErrorCode;
  readonly reason: "catalog_scope_mismatch" | null;

  constructor(
    code: SourcePreviewErrorCode,
    reason: "catalog_scope_mismatch" | null = null,
  ) {
    super(code);
    this.name = "SourcePreviewResponseError";
    this.code = code;
    this.reason = reason;
  }
}

export async function getTaskSourceFiles(
  taskId: string,
  expectedWorkflowRevision: number,
): Promise<TaskSourceFiles> {
  const catalog = await getJSON<TaskSourceFiles>(`/tasks/${encodeURIComponent(taskId)}/source-files`);
  if (catalog.task_id !== taskId) {
    throw new SourcePreviewResponseError("source_preview_load_failed");
  }
  if (catalog.workflow_revision !== expectedWorkflowRevision) {
    throw new SourcePreviewResponseError("source_preview_load_failed", "catalog_scope_mismatch");
  }
  return catalog;
}

export function isSourcePreviewCatalogMismatch(error: unknown): boolean {
  return error instanceof SourcePreviewResponseError
    && error.reason === "catalog_scope_mismatch";
}

export async function loadSourcePreviewFile(
  taskId: string,
  descriptor: SourceFileDescriptor,
): Promise<Blob> {
  if (!descriptor.file_id || descriptor.status !== "available") {
    throw new SourcePreviewResponseError("source_preview_unavailable");
  }
  const blob = await getBlob(
    `/tasks/${encodeURIComponent(taskId)}/source-files/${encodeURIComponent(descriptor.file_id)}/content`,
  );
  const responseKind = inferSourcePreviewKind("", blob.type);
  if (responseKind === "unsupported") {
    throw new SourcePreviewResponseError("source_preview_unsupported_type");
  }
  if (
    responseKind !== descriptor.preview_kind
    || blob.type.trim().toLowerCase() !== descriptor.mime_type?.trim().toLowerCase()
    || (descriptor.size_bytes !== null && blob.size !== descriptor.size_bytes)
  ) {
    throw new SourcePreviewResponseError("source_preview_load_failed");
  }
  return blob;
}

export function sourcePreviewErrorCode(error: unknown): SourcePreviewErrorCode {
  if (error instanceof SourcePreviewResponseError) return error.code;
  const code = getAPIErrorCode(error);
  if (code && SOURCE_PREVIEW_ERROR_CODES.has(code as SourcePreviewErrorCode)) {
    return code as SourcePreviewErrorCode;
  }
  const normalized = normalizeAPIError(error);
  if (normalized instanceof APIError) {
    if (normalized.status === 404) return "source_preview_not_found";
    if (normalized.status === 409) return "source_preview_processing";
    if (normalized.status === 410) return "source_preview_unavailable";
    if (normalized.status === 415) return "source_preview_unsupported_type";
    if (normalized.status === 503) return "source_preview_storage_unavailable";
  }
  return "source_preview_load_failed";
}

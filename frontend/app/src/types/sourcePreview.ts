export type SourceFileStatus = "available" | "processing" | "unavailable";

export type SourcePreviewKind = "pdf" | "image" | "unsupported";

export type SourceUnavailableReason =
  | "unsupported_type"
  | "not_persisted"
  | "storage_unavailable"
  | "missing";

export interface SourceFileDescriptor {
  source_id: string | null;
  file_id: string | null;
  display_name: string;
  mime_type: string | null;
  size_bytes: number | null;
  status: SourceFileStatus;
  preview_kind: SourcePreviewKind;
  unavailable_reason?: SourceUnavailableReason | null;
}

export interface TaskSourceFiles {
  task_id: string;
  workflow_revision: number;
  problem_source: SourceFileDescriptor | null;
  submission_sources: Record<string, SourceFileDescriptor>;
}

export type SourcePreviewLoadState = "idle" | "loading" | "ready" | "error";

export type SourcePreviewErrorCode =
  | "source_preview_not_found"
  | "source_preview_processing"
  | "source_preview_unavailable"
  | "source_preview_unsupported_type"
  | "source_preview_storage_unavailable"
  | "source_preview_load_failed";

export type SourcePreviewTriggerState = "ready" | "processing" | "unavailable";

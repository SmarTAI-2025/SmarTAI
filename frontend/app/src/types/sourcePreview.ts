export type SourceFileStatus = "available" | "processing" | "cleanup_pending" | "unavailable";

export type SourcePreviewKind = "pdf" | "image" | "unsupported";

export type SourceUnavailableReason =
  | "unsupported_type"
  | "not_persisted"
  | "storage_unavailable"
  | "missing"
  | "cleanup_pending"
  | "task_finalized"
  | "storage_delete_failed";

export interface SourceStorageUsageCore {
  used_bytes: number;
  limit_bytes: number;
  available_bytes: number;
  available_source_bytes: number;
  cleanup_pending_bytes: number;
  retrying_cleanup_bytes: number;
  reserved_bytes: number;
  cleanup_pending_count: number;
  retrying_cleanup_count: number;
}

export interface SourceStorageUsage extends SourceStorageUsageCore {
  scope: "task_originals";
  knowledge_storage_included: false;
}

export interface SourceCleanupSummary {
  status: "not_requested" | "pending" | "retrying" | "completed";
  total_count: number;
  deleted_count: number;
  pending_count: number;
  retrying_count: number;
  pending_bytes: number;
  retrying_bytes: number;
  automatic_retry: boolean;
}

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
  source_storage?: SourceStorageUsageCore;
  source_cleanup?: SourceCleanupSummary;
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
  | "source_preview_load_failed"
  | "source_cleanup_pending"
  | "source_unavailable_task_finalized"
  | "source_unavailable_missing";

export type SourcePreviewTriggerState = "ready" | "processing" | "cleanup_pending" | "unavailable";

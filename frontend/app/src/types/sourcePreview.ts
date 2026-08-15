export type SourceFileStatus = "available" | "processing" | "unavailable";

export type SourcePreviewKind = "pdf" | "image" | "unsupported";

export type SourceUnavailableReason =
  | "task_finalized"
  | "unsupported_type"
  | "not_persisted"
  | "missing";

export interface SourceFileDescriptor {
  file_id: string;
  display_name: string;
  mime_type: string;
  status: SourceFileStatus;
  preview_kind: SourcePreviewKind;
  unavailable_reason?: SourceUnavailableReason | null;
}

export type SourcePreviewLoadState = "idle" | "loading" | "ready" | "error";

export interface KBDoc {
  doc_id: string;
  filename: string;
  sha256?: string;
  chunk_count: number;
  embedder?: string;
  uploaded_at?: number;
  source_kind?: "upload" | "library";
  library_material_id?: string | null;
  saved_to_library?: boolean;
}

export interface KBListResponse {
  docs: KBDoc[];
}

export interface KBUploadResponse {
  status: "started" | "already_done";
  task_id: string;
  doc_id: string;
  filename: string;
  chunk_count: number;
  embedder?: string;
  workflow_revision: number;
  source_kind?: "upload" | "library";
  library_material_id?: string | null;
  saved_to_library?: boolean;
  saved_material_id?: string | null;
  saved_material_created?: boolean;
}

export interface KBDeleteResponse {
  status: "success";
  doc_id: string;
  workflow_revision: number;
}

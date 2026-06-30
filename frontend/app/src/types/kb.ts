export interface KBDoc {
  doc_id: string;
  filename: string;
  sha256?: string;
  chunk_count: number;
  embedder?: string;
  created_at?: number;
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
}

export interface KBDeleteResponse {
  status: "success";
  doc_id: string;
}

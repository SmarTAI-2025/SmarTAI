export interface PersonalKnowledgeDocument {
  id: string;
  title: string;
  original_name: string;
  content_type?: string | null;
  size_bytes: number;
  sha256: string;
  status: "processing" | "ready" | "failed" | string;
  parser_version: string;
  chunk_count: number;
  error_code?: string | null;
  created_at: number;
  updated_at: number;
}

export interface PersonalKnowledgeListResponse {
  documents: PersonalKnowledgeDocument[];
}

export interface KnowledgeStorageUsage {
  used_bytes: number;
  limit_bytes: number;
  available_bytes: number;
  available_document_bytes: number;
  cleanup_pending_bytes: number;
  retrying_cleanup_bytes: number;
  reserved_bytes: number;
  cleanup_pending_count: number;
  retrying_cleanup_count: number;
  reserved_count: number;
}

export interface DeletePersonalKnowledgeResponse {
  status: "deleted" | "deletion_pending";
  id: string;
  cleanup_operation_id?: string | null;
}

export interface KnowledgeIngestionSummary {
  id?: string;
  status?: string;
  total_pages?: number | null;
  processed_pages?: number;
  searchable_pages?: number;
  partially_searchable_pages?: number;
  blank_pages?: number;
  failed_pages?: number;
  warning_pages?: number;
  coverage_complete?: boolean;
  unit?: "page" | "section" | "slide";
  error_code?: string | null;
}

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
  ingestion?: KnowledgeIngestionSummary;
  content_version?: string;
}

export interface PersonalKnowledgeListResponse {
  documents: PersonalKnowledgeDocument[];
}

export type KnowledgeActivityFilter = "all" | "active" | "attention" | "completed";

export interface KnowledgeActivityResponse {
  items: (PersonalKnowledgeDocument & { activity_status: string })[];
  total: number;
  active_count: number;
  counts: Partial<Record<Exclude<KnowledgeActivityFilter, "all">, number>>;
  page: number;
  page_size: number;
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

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

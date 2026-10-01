export interface KnowledgeCitation {
  citation_id: string;
  chunk_id: string;
  document_id: string;
  content_version: string;
  source_sha256: string;
  original_name: string;
  page_number?: number | null;
  unit?: string;
  start?: number | null;
  end?: number | null;
  confidence?: string;
  warning_codes?: string[];
  coverage_complete?: boolean;
}

export interface KnowledgeMatch {
  content: string;
  source: string;
  score: number;
  citation: KnowledgeCitation;
}

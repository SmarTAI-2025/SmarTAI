import type { TaskTag } from "./history";

export type CatalogMatchKind = "exact" | "related";

export interface Course {
  id: string;
  name: string;
  code: string;
  description: string;
  teacher_id: string;
  student_count: number;
  created_at: number;
}

export interface CatalogCandidate<T> {
  item: T;
  match_kind: CatalogMatchKind;
  score: number;
  reason: string;
}

export interface CatalogSearchResponse<T> {
  items: CatalogCandidate<T>[];
  total: number;
  page: number;
  page_size: number;
}

export interface CreateCourseResponse extends Course {
  created: boolean;
}

export type TagCandidate = CatalogCandidate<TaskTag>;

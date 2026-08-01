import type { Course } from "./catalog";

export type CourseMaterialCategory = "textbook" | "answer" | "lecture" | "rubric" | "other";
export type CourseMaterialMatchKind = "exact" | "related";

export interface CourseMaterial {
  material_id: string;
  course_id: string | null;
  group_id: string | null;
  filename: string;
  category: CourseMaterialCategory;
  labels: string[];
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: number;
  updated_at: number;
  last_used_at: number | null;
  task_reference_count: number;
  parse_status: "ready";
  group_name: string | null;
  course_name: string | null;
  course_code: string | null;
  match_kind: CourseMaterialMatchKind | null;
  match_score: number | null;
  match_reason: string | null;
}

export interface CourseMaterialGroup {
  group_id: string;
  name: string;
  course_id: string | null;
  material_count: number;
  created_at: number;
  updated_at: number;
  course_name: string | null;
  course_code: string | null;
  match_kind?: CourseMaterialMatchKind;
  match_score?: number;
  match_reason?: string;
}

export interface CourseMaterialListParams {
  q?: string;
  course_id?: string;
  group_id?: string;
  category?: CourseMaterialCategory;
  page?: number;
  page_size?: number;
}

export interface CourseMaterialListResponse {
  items: CourseMaterial[];
  total: number;
  page: number;
  page_size: number;
  summary: {
    materials: number;
    groups: number;
    referenced: number;
    parsed: number;
  };
  storage: "local" | "s3" | "memory" | string;
  capabilities: {
    durable: boolean;
    ocr: boolean;
    accepted_types: string[];
  };
}

export interface CourseMaterialGroupListResponse {
  items: CourseMaterialGroup[];
  total: number;
}

export interface UploadCourseMaterialInput {
  file: File;
  courseId?: string;
  groupId?: string;
  category: CourseMaterialCategory;
  labels: string[];
}

export interface UpdateCourseMaterialInput {
  materialId: string;
  body: {
    filename?: string;
    course_id?: string | null;
    group_id?: string | null;
    category?: CourseMaterialCategory;
    labels?: string[];
  };
}

export interface CreateCourseMaterialGroupInput {
  name: string;
  course_id?: string | null;
  force_create?: boolean;
}

export interface UpdateCourseMaterialGroupInput {
  groupId: string;
  body: {
    name?: string;
    course_id?: string | null;
  };
}

export interface DeleteCourseMaterialResponse {
  status: "success";
  material_id: string;
  detached_task_references: number;
}

export interface DeleteCourseMaterialGroupResponse {
  status: "success";
  group_id: string;
  moved_to_ungrouped: number;
}

export interface CourseLibraryDialogData {
  courses: Course[];
  groups: CourseMaterialGroup[];
}

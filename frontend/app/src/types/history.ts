import type { TaskLite, TaskStatus } from "./task";

export type TagColor = "slate" | "blue" | "teal" | "green" | "amber" | "rose" | "violet";

export interface TaskTag {
  id: string;
  name: string;
  normalized_name: string;
  color: TagColor;
  owner_id: string;
  created_at: number;
  updated_at: number;
  usage_count?: number;
}

export interface HistoryCourseFacet {
  id: string;
  name: string;
  code?: string;
}

export interface HistoryFacets {
  semesters: string[];
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  statuses: Partial<Record<TaskStatus, number>>;
}

export type HistorySort =
  | "updated_desc"
  | "updated_asc"
  | "created_desc"
  | "created_asc"
  | "name_asc"
  | "name_desc"
  | "attention_first"
  | "stage_asc"
  | "stage_desc";

export interface TaskHistoryQuery {
  page: number;
  page_size: 25 | 50 | 100;
  q?: string;
  semester_id?: string;
  course_id?: string;
  tag_ids?: string[];
  statuses?: TaskStatus[];
  unfinished?: boolean;
  needs_attention?: boolean;
  sort: HistorySort;
}

export interface TaskHistoryResponse {
  items: TaskLite[];
  total: number;
  page: number;
  page_size: number;
  available_facets?: HistoryFacets;
  facets?: HistoryFacets;
}

export interface HistoryInterpretCondition {
  field: string;
  label: string;
  value: string | string[] | boolean | number | null;
}

export interface HistoryInterpretAmbiguity {
  fragment: string;
  message: string;
  candidates?: Array<string | { id?: string; name?: string; label?: string }>;
}

export interface HistoryInterpretation {
  filters: {
    q?: string;
    semester_id?: string;
    course_id?: string;
    tag_ids?: string[];
    statuses?: TaskStatus[];
    unfinished?: boolean;
    needs_attention?: boolean;
  };
  sort?: HistorySort;
  explanation: string;
  conditions: HistoryInterpretCondition[];
  ambiguities: HistoryInterpretAmbiguity[];
  source?: string;
  query_id?: string;
  progress?: unknown;
}

export interface CreateTagResponse extends TaskTag {
  created: boolean;
}

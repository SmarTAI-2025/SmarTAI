export type AnalyticsMode = "filter" | "summary" | "chart";

export interface FilterAnalyticsResult {
  mode: "filter";
  student_ids: string[];
  explanation: string;
}

export type FilterIntentSurface = "student_analysis" | "review_overview" | "question_analysis"
  | "question_preparation" | "submission_review" | "student_answer_review";

export interface FilterIntentResult {
  execution?: GroundedAskExecution;
  recognized: boolean;
  min_score_percent: number | null;
  max_score_percent: number | null;
  pass_status: "pass" | "fail" | "unscored" | null;
  low_confidence: boolean;
  review_status: "pending" | "confirmed" | "none" | null;
  disagreement: boolean;
  annotated: boolean;
  sort: "score_asc" | "score_desc" | "confidence_asc" | "confidence_desc"
    | "review_asc" | "review_desc" | "name_asc" | "name_desc" | "id_asc" | "id_desc"
    | "question" | "question_desc" | "max_score_asc" | "max_score_desc"
    | "type_asc" | "type_desc" | "coverage_asc" | "coverage_desc" | null;
  question_tokens: string[];
  question_types?: string[];
  max_average_confidence?: number | null;
  missing_knowledge?: boolean;
  min_max_score?: number | null;
  max_max_score?: number | null;
  preparation_status?: "attention" | "ready" | "low_confidence" | "source_conflict" | "parse_anomaly" | null;
  material_field?: "stem" | "answer" | "rubric" | "tests" | null;
  material_status?: "missing" | "ready" | "generated" | "recognized" | null;
  submission_status?: "review" | "missing" | "identity" | "recognized" | "reviewed" | null;
  text_terms: string[];
  explanation: string;
}

export interface SummaryAnalyticsResult {
  mode: "summary";
  markdown: string;
}

export type ChartTraceType = "line" | "bar" | "scatter" | "pie" | "histogram" | "box";

export interface ChartTrace {
  type: ChartTraceType;
  x?: Array<string | number | null>;
  y?: Array<string | number | null>;
  labels?: string[];
  values?: Array<number | null>;
  name?: string;
}

export interface ChartLayout {
  title?: string;
  xaxis_title?: string;
  yaxis_title?: string;
  height?: number;
  barmode?: "group" | "stack" | "relative";
}

export interface ChartAnalyticsResult {
  execution?: GroundedAskExecution;
  mode: "chart";
  title: string;
  rationale: string;
  traces: ChartTrace[];
  layout: ChartLayout;
}

export type AnalyticsResult =
  | FilterAnalyticsResult
  | SummaryAnalyticsResult
  | ChartAnalyticsResult
  | { mode: "query"; execution: GroundedAskExecution };

export interface QuestionBreakdownRow {
  student_id: string;
  student_name?: string;
  answer?: string;
  score: number;
  max_score: number;
  comment?: string;
  requires_human_review?: boolean;
  review_reasons?: string[];
}

export interface PerQuestionBreakdown {
  q_id: string;
  question?: string;
  stem?: string;
  max_score?: number;
  avg_score?: number;
  rows: QuestionBreakdownRow[];
  common_mistakes_md: string;
  [key: string]: unknown;
}


export interface GroundedAskExecution {
  recognized: boolean;
  kind: "students" | "questions" | "tasks" | "table" | "chart" | "clarification";
  explanation: string;
  data: { columns: string[]; rows: Array<Array<string | number | null>> };
  selection: { kind: "students" | "questions" | "tasks"; ids: string[] } | null;
  chart: ChartAnalyticsResult | null;
  candidates?: Array<{ kind: string; text: string; task_id: string; student_id?: string; student_name?: string; q_id?: string; number?: string; name?: string }>;
  bindings?: Array<{ kind: string; text: string; role: string; rows: Array<Record<string, string | number | null>> }>;
  tasks?: import("@/types").TaskLite[];
  sql?: string;
  parameters?: Record<string, string | number | null>;
  assumptions?: string[];
  fingerprint?: string;
  source_counts?: Record<string, number>;
  scope?: string;
}

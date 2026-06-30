export type AnalyticsMode = "filter" | "summary" | "chart";

export interface FilterAnalyticsResult {
  mode: "filter";
  student_ids: string[];
  explanation: string;
}

export interface SummaryAnalyticsResult {
  mode: "summary";
  markdown: string;
}

export type ChartTraceType = "bar" | "scatter" | "pie" | "histogram" | "box";

export interface ChartTrace {
  type: ChartTraceType;
  x?: Array<string | number>;
  y?: Array<string | number>;
  labels?: string[];
  values?: number[];
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
  mode: "chart";
  title: string;
  rationale: string;
  traces: ChartTrace[];
  layout: ChartLayout;
}

export type AnalyticsResult =
  | FilterAnalyticsResult
  | SummaryAnalyticsResult
  | ChartAnalyticsResult;

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

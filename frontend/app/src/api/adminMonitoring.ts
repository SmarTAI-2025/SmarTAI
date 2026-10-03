import { getJSON, postJSON } from "./client";

export interface HealthCheck {
  status: "ok" | "error" | "unavailable";
  check: string;
  latency_ms?: number | null;
  backend?: string;
}
export interface CapacitySample {
  at: number;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  application_bytes: number | null;
}
export interface MonitoringResult {
  metric_version: string;
  as_of: number;
  health: Record<"application" | "database" | "storage" | "public_api" | "providers", HealthCheck>;
  application_storage: {
    status: "available" | "unavailable";
    bytes: number | null;
    objects: number | null;
    by_backend: Array<{ backend: string; bytes: number; objects: number }>;
    knowledge_reserved_bytes: number | null;
  };
  disk: {
    status: "available" | "unavailable";
    scope: "application_mount" | "configured_host_mount";
    total_bytes: number | null;
    used_bytes: number | null;
    free_bytes: number | null;
    used_ratio: number | null;
    host_status: "unavailable" | "operator_configured";
  };
  history: {
    status: "available" | "unavailable";
    coverage_start: number | null;
    sample_interval_seconds: number;
    retention_days: number;
    samples: CapacitySample[];
    projection: { status: "available" | "unavailable"; reason: string; bytes_per_day: number | null; days_remaining: number | null };
  };
  recommendation: { level: "normal" | "warning" | "critical" | "unavailable"; message: string };
  notes: string[];
}
export interface RetentionCell { numerator: number; denominator: number; pending: number; rate: number | null }
export interface AdoptionResult {
  metric_version: string;
  as_of: number;
  start: number;
  end: number;
  timezone: string;
  coverage: { status: "partial" | "unavailable"; first_retained_event_at: number | null; last_retained_event_at: number | null; event_count: number; notes: string[] };
  filters: { event_role: string; success: boolean; account_cohort: string; environment: string };
  summary: { current_teacher_accounts: number; new_users: number | null; login_users: number | null; business_users: number | null; business_actions: number | null; actions_per_business_user: number | null };
  series: Array<{ date: string; start: number; end: number; status: string; new_users: number | null; login_users: number | null; business_users: number | null; business_actions: number | null }>;
  frequency: Array<{ label: string; users: number }>;
  retention: Array<{ date: string; users: number; w1: RetentionCell; w4: RetentionCell }>;
  definitions: { growth: string; activity: string; frequency: string; retention: string; business_events: string[] };
}

export const getAdminMonitoring = (signal?: AbortSignal) => getJSON<MonitoringResult>("/admin/monitoring", { signal });
export const queryAdminAdoption = (input: { start: number; end?: number; timezone: string }, signal?: AbortSignal) =>
  postJSON<AdoptionResult>("/admin/analytics/query", input, { signal });

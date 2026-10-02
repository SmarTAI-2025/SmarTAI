/**
 * Admin API client: user management + invitations. Mirrors backend/api/admin.py.
 */
import { apiClient } from "./client";
import type { AdminUser, Invite } from "@/types/education";

export interface AdminUsersPage {
  items: AdminUser[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export async function adminListUsers(params?: {
  role?: string;
  is_active?: boolean;
  search?: string;
  page?: number;
  page_size?: number;
}): Promise<AdminUser[] | AdminUsersPage> {
  const { data } = await apiClient.get<AdminUser[] | AdminUsersPage>("/admin/users", { params });
  return data;
}

export async function adminSetActive(userId: string, isActive: boolean, reason = "manual_review", note = ""): Promise<AdminUser> {
  const { data } = await apiClient.patch<AdminUser>(
    `/admin/users/${userId}/status`,
    { is_active: isActive, reason, note },
    { headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
  return data;
}

export async function adminRevokeSessions(userId: string, reason = "manual_review"): Promise<{ status: string; user_id: string }> {
  const { data } = await apiClient.post(`/admin/users/${userId}/sessions/revoke`, { reason }, { headers: { "Idempotency-Key": crypto.randomUUID() } });
  return data;
}

export async function adminRequestPasswordReset(userId: string, reason = "account_support"): Promise<{ status: string; user_id: string }> {
  const { data } = await apiClient.post(`/admin/users/${userId}/password-reset`, { reason }, { headers: { "Idempotency-Key": crypto.randomUUID() } });
  return data;
}

export interface AdminOverview {
  as_of: number;
  users: { total: number; active: number; with_email: number };
  education: { courses: number; assignments: number; grading_runs: number };
  usage: AdminMetricsResult;
  shared_pool: { enabled: boolean; daily_request_limit: number; daily_estimated_token_limit: number; source: string };
}

export interface AdminMetricDefinition {
  key: string;
  label: string;
  description: string;
  event_names: string[];
}

export interface AdminMetricsResult {
  status: "not_collected" | "available";
  start: number;
  end: number;
  granularity: "day" | "week";
  metrics: string[];
  metric_status: Record<string, "not_collected" | "available">;
  series: Array<Record<string, string | number | null>>;
  totals: Record<string, number | null>;
  event_count: number;
}

export interface AdminAuditEntry {
  id: string;
  actor_id: string;
  actor_name?: string | null;
  target_name?: string | null;
  target_user_id: string | null;
  action: string;
  reason: string;
  note: string;
  result: string;
  before_state?: Record<string, unknown> | null;
  after_state?: Record<string, unknown> | null;
  created_at: number;
}

export async function adminOverview(): Promise<AdminOverview> {
  const { data } = await apiClient.get<AdminOverview>("/admin/overview");
  return data;
}

export async function adminMetricsCatalog(): Promise<{ metrics: AdminMetricDefinition[] }> {
  const { data } = await apiClient.get<{ metrics: AdminMetricDefinition[] }>("/admin/metrics/catalog");
  return data;
}

export async function adminMetricsQuery(input: {
  start?: number;
  end?: number;
  granularity?: "day" | "week";
  metrics?: string[];
} = {}): Promise<AdminMetricsResult> {
  const { data } = await apiClient.post<AdminMetricsResult>("/admin/metrics/query", input);
  return data;
}

export async function adminListAudit(): Promise<AdminAuditEntry[]> {
  const { data } = await apiClient.get<AdminAuditEntry[]>("/admin/audit");
  return data;
}

export async function adminCreateInvite(input: {
  email?: string;
  role: "teacher" | "student" | "admin";
  course_id?: string | null;
  expires_in_hours?: number;
}): Promise<{ invite_code: string; role: string; expires_at: number }> {
  const { data } = await apiClient.post("/admin/invites", input);
  return data;
}

export async function adminListInvites(): Promise<Invite[]> {
  const { data } = await apiClient.get<Invite[]>("/admin/invites");
  return data;
}

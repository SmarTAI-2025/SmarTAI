import { apiClient } from "./client";

export type BusinessConfigKey = "allowed_email_domains" | "email_verification_resend_seconds" | "email_verification_hourly_email_limit" | "email_verification_hourly_ip_limit" | "unfinished_source_quota_bytes" | "knowledge_storage_quota_bytes";
export interface BusinessConfigField {
  effective: string | number;
  source: "user_override" | "global_override" | "settings" | "default";
  override: string | number | null;
  bounds: [number, number] | null;
}
export interface BusinessConfiguration {
  scope: "global" | "user";
  owner_id: string | null;
  version: number;
  global_version: number;
  fields: Partial<Record<BusinessConfigKey, BusinessConfigField>>;
  usage?: Partial<Record<BusinessConfigKey, { used_bytes: number; limit_bytes: number; available_bytes: number; reserved_bytes: number }>>;
  read_only?: {
    email_verification_expiry_seconds: number;
    password_recovery_independent_of_registration: boolean;
    shared_pool_daily_request_limit: number;
    shared_pool_daily_estimated_token_limit: number;
    history_query_llm_daily_limit: number;
    model_quota_note: string;
  };
}
export interface BusinessConfigUpdate {
  expected_version: number;
  expected_global_version?: number;
  changes: Partial<Record<BusinessConfigKey, string | number | null>>;
  reason: string;
}
function path(ownerId?: string) {
  return `/admin/business-config${ownerId ? `/users/${encodeURIComponent(ownerId)}` : ""}`;
}
export async function getBusinessConfig(ownerId?: string, signal?: AbortSignal) {
  const { data } = await apiClient.get<BusinessConfiguration>(path(ownerId), { signal });
  return data;
}
export async function saveBusinessConfig(input: BusinessConfigUpdate, key: string, ownerId?: string) {
  const { data } = await apiClient.patch<BusinessConfiguration>(path(ownerId), input, { headers: { "Idempotency-Key": key } });
  return data;
}

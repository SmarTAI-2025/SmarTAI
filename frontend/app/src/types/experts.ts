export type ProviderType = "openai" | "gemini" | "anthropic" | "zhipu";

export interface ExpertConfig {
  provider_id: string;
  provider_type: ProviderType | string;
  model: string;
  base_url?: string | null;
  enabled: boolean;
  display_name?: string | null;
  max_concurrent: number;
  rpm: number;
  scope?: "shared" | "owner";
  is_shared?: boolean;
  editable?: boolean;
  verification_status?: "unverified" | "verified" | "failed" | "platform_managed";
  last_checked_at?: string | null;
  verified_at?: string | null;
  verification_error_code?: string | null;
}

export interface AddExpertKeyRequest {
  provider_type: ProviderType;
  api_key: string;
  model: string;
  base_url?: string | null;
  display_name?: string | null;
  max_concurrent?: number;
  rpm?: number;
}

export interface ExpertMutationResponse {
  status: "success" | "not_found" | string;
  provider_id?: string;
  enabled?: boolean;
  verification_status?: ExpertConfig["verification_status"];
  message?: string;
}

export interface UpdateExpertRequest {
  api_key?: string | null;
  model: string;
  base_url?: string | null;
  display_name?: string | null;
  max_concurrent?: number;
  rpm?: number;
}

export interface ExpertVerificationResponse {
  status: "success";
  provider_id: string;
  verification_status: "verified";
  last_checked_at: string;
  verified_at: string;
}

export interface ProviderCatalogItem {
  provider_type: ProviderType;
  display_name: string;
  docs_url: string;
  console_url: string;
  usage_url: string;
}

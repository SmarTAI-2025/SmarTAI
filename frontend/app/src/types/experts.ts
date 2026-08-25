export type ProviderType =
  | "openai"
  | "gemini"
  | "anthropic"
  | "zhipu"
  | "deepseek"
  | "moonshot"
  | "qwen";

export type WireProtocol =
  | "openai_chat_completions"
  | "anthropic_messages"
  | "gemini_generate_content";

export type VerificationStatus =
  | "unverified"
  | "verified"
  | "failed"
  | "platform_managed";

export interface ExpertConfig {
  provider_id: string;
  provider_type: ProviderType | string;
  model: string;
  base_url?: string | null;
  endpoint_identity?: string | null;
  endpoint_descriptor?: string | null;
  wire_protocol?: WireProtocol | null;
  enabled: boolean;
  display_name?: string | null;
  configured_display_name?: string | null;
  resolved_display_name?: string | null;
  max_concurrent: number;
  rpm: number;
  scope?: "shared" | "owner";
  is_shared?: boolean;
  editable?: boolean;
  is_default?: boolean;
  supports_vision?: boolean;
  provider_kind?: "llm" | "ocr";
  credential_id?: string | null;
  verification_status?: VerificationStatus;
  last_checked_at?: string | null;
  verified_at?: string | null;
  verification_error_code?: string | null;
}

export interface AddExpertKeyRequest {
  provider_type: ProviderType;
  api_key: string;
  model: string;
  base_url?: string | null;
  wire_protocol?: WireProtocol | null;
  display_name?: string | null;
  max_concurrent?: number;
  rpm?: number;
}

export interface ExpertMutationResponse {
  status: "success" | "not_found" | string;
  provider_id?: string;
  enabled?: boolean;
  is_default?: boolean;
  verification_status?: ExpertConfig["verification_status"];
  base_url?: string | null;
  wire_protocol?: WireProtocol | null;
  message?: string;
}

export interface UpdateExpertRequest {
  api_key?: string | null;
  model: string;
  base_url?: string | null;
  wire_protocol?: WireProtocol | null;
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

export interface SetDefaultExpertResponse {
  status: "success";
  provider_id: string;
  is_default: true;
}

export interface ProviderCatalogItem {
  provider_type: ProviderType;
  display_name: string;
  docs_url?: string;
  console_url?: string;
  usage_url?: string;
  default_base_url?: string | null;
  wire_protocol: WireProtocol;
  custom_base_url_supported: boolean;
  custom_base_url_enabled: boolean;
  base_url_editable?: boolean;
}

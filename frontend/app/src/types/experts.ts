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
  api_key?: string;
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
  message?: string;
}

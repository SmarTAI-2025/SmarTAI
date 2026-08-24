export interface ModelPresentationSource {
  provider_id: string;
  provider_type: string;
  model: string;
  display_name?: string | null;
  configured_display_name?: string | null;
  resolved_display_name?: string | null;
  endpoint_descriptor?: string | null;
}

const OPAQUE_PROVIDER_LABEL = /^(?:[a-f0-9]{24,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|(?:cfg|config|provider)[_-][a-z0-9_-]{12,})$/i;

const PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Claude (Anthropic)",
  gemini: "Google Gemini",
  openai: "GPT (OpenAI)",
  zhipu: "Zhipu (智谱)",
  deepseek: "DeepSeek",
  moonshot: "Kimi (Moonshot)",
  qwen: "Qwen (通义千问)",
};

export function providerDisplayName(providerType: string): string {
  const normalized = providerType.trim().toLowerCase();
  return PROVIDER_NAMES[normalized] ?? (providerType.trim() || "Model provider");
}

export function hasFriendlyModelName(expert: ModelPresentationSource): boolean {
  const displayName = (
    "configured_display_name" in expert
      ? expert.configured_display_name
      : expert.display_name
  )?.trim();
  if (!displayName) return false;
  if (displayName === expert.provider_id.trim()) return false;
  return !OPAQUE_PROVIDER_LABEL.test(displayName);
}

export function modelDisplayName(expert: ModelPresentationSource): string {
  const resolved = expert.resolved_display_name?.trim();
  if (resolved) return resolved;
  if (hasFriendlyModelName(expert)) return expert.display_name!.trim();
  return expert.model.trim() || providerDisplayName(expert.provider_type);
}

export function modelSecondaryLabel(expert: ModelPresentationSource): string {
  const provider = providerDisplayName(expert.provider_type);
  const model = expert.model.trim();
  const endpoint = expert.endpoint_descriptor?.trim();
  const identity = hasFriendlyModelName(expert) && model
    ? `${provider} · ${model}`
    : provider;
  return endpoint ? `${identity} · ${endpoint}` : identity;
}

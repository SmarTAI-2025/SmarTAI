export interface ModelPresentationSource {
  provider_id: string;
  provider_type: string;
  model: string;
  display_name?: string | null;
}

const OPAQUE_PROVIDER_LABEL = /^(?:[a-f0-9]{24,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|(?:cfg|config|provider)[_-][a-z0-9_-]{12,})$/i;

const PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  openai: "OpenAI",
  zhipu: "Zhipu AI",
};

export function providerDisplayName(providerType: string): string {
  const normalized = providerType.trim().toLowerCase();
  return PROVIDER_NAMES[normalized] ?? (providerType.trim() || "Model provider");
}

export function hasFriendlyModelName(expert: ModelPresentationSource): boolean {
  const displayName = expert.display_name?.trim();
  if (!displayName) return false;
  if (displayName === expert.provider_id.trim()) return false;
  return !OPAQUE_PROVIDER_LABEL.test(displayName);
}

export function modelDisplayName(expert: ModelPresentationSource): string {
  if (hasFriendlyModelName(expert)) return expert.display_name!.trim();
  return expert.model.trim() || providerDisplayName(expert.provider_type);
}

export function modelSecondaryLabel(expert: ModelPresentationSource): string {
  const provider = providerDisplayName(expert.provider_type);
  return hasFriendlyModelName(expert) && expert.model.trim()
    ? `${provider} · ${expert.model.trim()}`
    : provider;
}

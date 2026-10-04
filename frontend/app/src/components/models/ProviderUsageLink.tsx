import { useProviderCatalog } from "@/api/hooks/experts";
import { useGradingSetup } from "@/api/hooks/gradingSetup";
import type { Locale } from "@/i18n/messages";
import type { ExpertConfig, ProviderCatalogItem } from "@/types";

type UsageProvider = Pick<ExpertConfig, "provider_id" | "provider_type" | "base_url" | "is_shared"> & { scope?: string };
type Selection = { providerIds: readonly string[]; providers: readonly UsageProvider[] };
export type ProviderUsageContext = Selection | { gradingTaskId: string };

/** Use the configured endpoint, never the model name, to identify a console. */
export function selectedProviderUsageUrl(selection: Selection, catalog: readonly ProviderCatalogItem[]): string | null {
  const ids = [...new Set(selection.providerIds.filter(Boolean))];
  if (!ids.length) return null;
  const links = new Set<string>();
  for (const id of ids) {
    const provider = selection.providers.find(item => item.provider_id === id);
    if (!provider || provider.is_shared || provider.scope === "shared") return null;
    const entry = catalog.find(item => item.provider_type === provider.provider_type);
    if (!entry?.usage_url || !entry.default_base_url) return null;
    try {
      const official = new URL(entry.default_base_url);
      const endpoint = new URL(provider.base_url || entry.default_base_url);
      const usage = new URL(entry.usage_url);
      if (endpoint.protocol !== "https:" || endpoint.origin !== official.origin
        || endpoint.username || endpoint.password || usage.protocol !== "https:"
        || usage.username || usage.password) return null;
      links.add(usage.href);
    } catch { return null; }
  }
  return links.size === 1 ? [...links][0] : null;
}

export function ProviderUsageLink({ context, locale }: { context: ProviderUsageContext; locale: Locale }) {
  return "gradingTaskId" in context
    ? <GradingUsageLink taskId={context.gradingTaskId} locale={locale} />
    : <SelectedUsageLink selection={context} locale={locale} />;
}

function GradingUsageLink({ taskId, locale }: { taskId: string; locale: Locale }) {
  const { data } = useGradingSetup(taskId);
  if (!data?.grading_setup) return null;
  return <SelectedUsageLink locale={locale} selection={{
    providerIds: data.grading_setup.selected_provider_ids,
    providers: data.available_experts,
  }} />;
}

function SelectedUsageLink({ selection, locale }: { selection: Selection; locale: Locale }) {
  // Reuse the existing one-hour catalog cache. No provider/usage API is called.
  const { data } = useProviderCatalog();
  const href = selectedProviderUsageUrl(selection, data ?? []);
  return href ? <p className="mt-2 text-xs leading-5">
    <a className="text-primary underline" href={href} target="_blank" rel="noreferrer">
      {locale === "zh-CN" ? "查看用量" : "View usage"}
    </a>
  </p> : null;
}

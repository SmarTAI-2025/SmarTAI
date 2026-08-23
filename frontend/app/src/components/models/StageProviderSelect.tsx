import { ProviderIcon } from "@/components/models/ProviderIcon";
import { cn } from "@/lib/cn";
import { modelDisplayName, modelSecondaryLabel } from "@/lib/modelPresentation";
import type { ExpertConfig } from "@/types";

export function StageProviderSelect({
  id,
  label,
  hint,
  experts,
  value,
  disabled = false,
  locale,
  onChange,
  className,
}: {
  id: string;
  label: string;
  hint: string;
  experts: ExpertConfig[];
  value: string;
  disabled?: boolean;
  locale: "zh-CN" | "en-US";
  onChange: (providerId: string) => void;
  className?: string;
}) {
  const selected = experts.find((expert) => expert.provider_id === value);
  const zh = locale === "zh-CN";

  return (
    <section className={cn("rounded-[10px] border bg-card px-4 py-3", className)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <label htmlFor={id} className="text-sm font-semibold text-foreground">
            {label}
          </label>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{hint}</p>
        </div>
        <div className="flex w-full min-w-0 items-center gap-2 sm:w-[360px]">
          {selected ? <ProviderIcon providerType={selected.provider_type || "unknown"} /> : null}
          <select
            id={id}
            value={value}
            disabled={disabled || experts.length === 0}
            onChange={(event) => onChange(event.target.value)}
            className="h-10 min-w-0 flex-1 rounded-[7px] border bg-background px-3 text-sm font-semibold text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {experts.length === 0 ? (
              <option value="">{zh ? "暂无已启用模型" : "No enabled model"}</option>
            ) : null}
            {experts.map((expert) => (
              <option key={expert.provider_id} value={expert.provider_id}>
                {safeModelDisplayName(expert)} · {safeModelSecondaryLabel(expert)}
                {expert.is_default ? (zh ? " · 默认" : " · Default") : ""}
              </option>
            ))}
          </select>
        </div>
      </div>
    </section>
  );
}

function safeModelDisplayName(expert: ExpertConfig) {
  return modelDisplayName({
    ...expert,
    provider_type: expert.provider_type || "unknown",
    model: expert.model || expert.provider_id,
  });
}

function safeModelSecondaryLabel(expert: ExpertConfig) {
  return modelSecondaryLabel({
    ...expert,
    provider_type: expert.provider_type || "unknown",
    model: expert.model || expert.provider_id,
  });
}

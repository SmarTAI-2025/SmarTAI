import { ChevronDown, KeyRound, Loader2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import { HeaderPopover } from "@/components/layout/HeaderPopover";
import { ProviderIcon } from "@/components/models/ProviderIcon";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { modelDisplayName, modelSecondaryLabel } from "@/lib/modelPresentation";
import type { ExpertConfig } from "@/types";

interface ModelStatusMenuProps {
  experts: ExpertConfig[];
  enabledCount: number;
  isLoading: boolean;
  isError: boolean;
  isFetching: boolean;
  onRetry: () => void;
}

/** Compact model readiness summary shown to the left of the account menu. */
export function ModelStatusMenu({
  experts,
  enabledCount,
  isLoading,
  isError,
  isFetching,
  onRetry,
}: ModelStatusMenuProps) {
  const { locale, t } = useI18n();
  const visibleExperts = experts.slice(0, 3);
  const countLabel = isLoading || isError ? "—" : `${enabledCount}/${experts.length}`;

  return (
    <HeaderPopover
      ariaLabel={t("modelMenu")}
      triggerClassName="gap-1.5 px-2.5 sm:px-3"
      panelClassName="max-sm:fixed max-sm:left-4 max-sm:right-4 max-sm:top-[72px] max-sm:w-auto sm:w-[22rem]"
      renderTrigger={(open) => (
        <>
          <span
            aria-hidden="true"
            className={cn(
              "h-2 w-2 rounded-full bg-slate-300",
              !isLoading && !isError && enabledCount > 0 && "bg-emerald-500",
              isError && "bg-danger",
            )}
          />
          <span className="sm:hidden">{countLabel}</span>
          <span className="hidden sm:inline">
            {locale === "zh-CN"
              ? `${countLabel} ${t("modelsAvailableSuffix")}`
              : isLoading || isError
                ? "Models: —"
                : `Models: ${countLabel} enabled`}
          </span>
          <ChevronDown
            aria-hidden="true"
            size={14}
            className={cn("transition-transform", open && "rotate-180")}
          />
        </>
      )}
    >
      {(close) => (
        <>
          <div className="px-2 pb-2 pt-1">
            <p className="text-sm font-semibold text-foreground">
              {t("currentModelConfiguration")}
            </p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {t("modelConfigurationDescription")}
            </p>
          </div>

          <div className="border-y py-1.5">
            {isLoading ? (
              <div className="flex items-center gap-2 px-2 py-3 text-xs text-muted-foreground">
                <Loader2 aria-hidden="true" size={15} className="animate-spin" />
                {t("modelsLoading")}
              </div>
            ) : null}
            {isError ? (
              <div className="flex items-center justify-between gap-3 px-2 py-2">
                <span className="text-xs text-danger">{t("modelsUnavailable")}</span>
                <button
                  type="button"
                  data-popover-focus
                  className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold text-primary outline-none hover:bg-primary/[0.08] focus-visible:ring-2 focus-visible:ring-ring"
                  disabled={isFetching}
                  onClick={onRetry}
                >
                  <RefreshCw
                    aria-hidden="true"
                    size={13}
                    className={isFetching ? "animate-spin" : undefined}
                  />
                  {t("retry")}
                </button>
              </div>
            ) : null}
            {!isLoading && !isError && visibleExperts.length === 0 ? (
              <p className="px-2 py-3 text-xs text-muted-foreground">
                {t("noModelsConfigured")}
              </p>
            ) : null}
            {!isLoading && !isError
              ? visibleExperts.map((expert) => (
                  <div
                    key={expert.provider_id}
                    className="flex items-center gap-3 rounded-lg px-2 py-2"
                  >
                    <ProviderIcon
                      providerType={expert.provider_type}
                      className={cn("h-8 w-8 rounded-lg", !expert.enabled && "grayscale opacity-55")}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-semibold text-foreground">
                        {modelDisplayName(expert)}
                      </p>
                      <p className="mt-0.5 truncate text-xs text-muted-foreground">
                        {modelSecondaryLabel(expert)}
                      </p>
                    </div>
                    <span className="shrink-0 text-[11px] font-medium text-muted-foreground">
                      {compactExpertStatus(expert, locale)}
                    </span>
                  </div>
                ))
              : null}
            {!isLoading && !isError && experts.length > visibleExperts.length ? (
              <p className="px-2 py-1.5 text-xs text-muted-foreground">
                +{experts.length - visibleExperts.length} {locale === "zh-CN"
                  ? t("moreModels")
                  : experts.length - visibleExperts.length === 1 ? "more model" : "more models"}
              </p>
            ) : null}
          </div>

          <Link
            to="/settings/byok"
            data-popover-focus
            onClick={close}
            className="mt-1 flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-medium text-foreground outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
          >
            <KeyRound aria-hidden="true" size={16} className="text-muted-foreground" />
            {t("manageModels")}
          </Link>
        </>
      )}
    </HeaderPopover>
  );
}

function compactExpertStatus(expert: ExpertConfig, locale: "zh-CN" | "en-US") {
  if (!expert.enabled) return locale === "zh-CN" ? "已停用" : "Disabled";
  const status = expert.verification_status ?? (expert.is_shared ? "platform_managed" : "unverified");
  const labels = {
    verified: ["已验证", "Verified"],
    failed: ["验证失败", "Failed"],
    platform_managed: ["平台托管", "Platform"],
    unverified: ["未验证", "Unverified"],
  } satisfies Record<string, [string, string]>;
  return (labels[status] ?? labels.unverified)[locale === "zh-CN" ? 0 : 1];
}

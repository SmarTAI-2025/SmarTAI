import { Link } from "react-router-dom";
import { getAPIErrorCode } from "@/api/client";
import { useStageProviders } from "@/api/hooks/experts";
import { StageProviderSelect } from "@/components/models/StageProviderSelect";
import { sortByProvider } from "@/lib/modelPresentation";

const MODEL_ERRORS = new Set([
  "no_provider_configured", "recognition_provider_not_enabled", "provider_not_enabled",
  "provider_auth_failed", "provider_permission_denied", "provider_credentials_unavailable", "provider_credentials_required",
  "provider_quota_exceeded", "provider_zero_quota", "provider_daily_quota_exceeded", "shared_pool_daily_limit_reached", "shared_pool_sol_daily_limit_reached",
  "shared_pool_weekly_limit_reached", "shared_pool_budget_pending", "shared_pool_usage_unavailable", "shared_pool_billing_unavailable", "shared_pool_request_too_large",
  "provider_model_not_found", "provider_model_or_endpoint_not_found", "provider_request_failed", "provider_upstream_unavailable",
]);
export const isQuestionModelError = (error: unknown) => MODEL_ERRORS.has(getAPIErrorCode(error) ?? "");

export function QuestionPreparationModelSelect({ locale, value, onChange, disabled }: {
  locale: string; value?: string | null; onChange: (value: string) => void; disabled: boolean;
}) {
  const providers = useStageProviders();
  const zh = locale === "zh-CN";
  const models = sortByProvider((providers.data ?? []).filter(model => model.enabled && model.provider_kind !== "ocr"));
  const selected = (value?.startsWith("ocr:") ? null : value) || models.find(model => model.is_default)?.provider_id || "";
  return <div className="flex min-w-0 flex-wrap items-center gap-2" id="question-model-control">
    <StageProviderSelect
      allowByok id="question-preparation-model" label={zh ? "题目准备模型" : "Question preparation model"}
      hint="" experts={models} value={selected} disabled={disabled || providers.isPending}
      locale={zh ? "zh-CN" : "en-US"} onChange={onChange} className="grow"
      placeholder={zh ? "选择模型" : "Choose a model"}
    />
    <Link to="/settings/byok" className="text-xs text-primary underline">{zh ? "管理模型" : "Manage models"}</Link>
    {providers.isError ? <button type="button" onClick={() => void providers.refetch()} className="text-xs text-primary underline">{zh ? "重新加载" : "Reload"}</button> : null}
  </div>;
}

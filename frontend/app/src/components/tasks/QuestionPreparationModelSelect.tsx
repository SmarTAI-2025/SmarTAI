import { Link } from "react-router-dom";
import { getAPIErrorCode } from "@/api/client";
import { useStageProviders } from "@/api/hooks/experts";
import { modelDisplayName, sortByProvider } from "@/lib/modelPresentation";

const MODEL_ERRORS = new Set([
  "no_provider_configured", "recognition_provider_not_enabled", "provider_not_enabled",
  "provider_auth_failed", "provider_permission_denied", "provider_credentials_unavailable", "provider_credentials_required",
  "provider_quota_exceeded", "provider_zero_quota", "provider_daily_quota_exceeded", "shared_pool_daily_limit_reached",
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
    <label htmlFor="question-preparation-model" className="text-sm font-medium">{zh ? "题目处理模型" : "Question model"}</label>
    <select id="question-preparation-model" value={selected} onChange={event => onChange(event.target.value)}
      disabled={disabled || providers.isPending || !models.length}
      className="h-10 max-w-full rounded-md border bg-background px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/20 sm:max-w-72">
      {!models.some(model => model.provider_id === selected) ? <option value={selected} disabled>{zh ? "选择模型" : "Choose a model"}</option> : null}
      {models.map(model => <option key={model.provider_id} value={model.provider_id}>{modelDisplayName(model)}</option>)}
    </select>
    <Link to="/settings/byok" className="text-xs text-primary underline">{zh ? "管理模型" : "Manage models"}</Link>
    {providers.isError ? <button type="button" onClick={() => void providers.refetch()} className="text-xs text-primary underline">{zh ? "重新加载" : "Reload"}</button> : null}
  </div>;
}

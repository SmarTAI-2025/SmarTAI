import { Link } from "react-router-dom";
import { getAPIErrorCode } from "@/api/client";
import { useStageProviders } from "@/api/hooks/experts";
import { StageProviderSelect } from "@/components/models/StageProviderSelect";

const MODEL_ERRORS = new Set([
  "no_provider_configured", "recognition_provider_not_enabled", "provider_not_enabled",
  "provider_auth_failed", "provider_permission_denied", "provider_credentials_unavailable", "provider_credentials_required",
  "provider_quota_exceeded", "provider_zero_quota", "provider_daily_quota_exceeded", "shared_pool_daily_limit_reached",
  "provider_model_not_found", "provider_model_or_endpoint_not_found", "provider_request_failed", "provider_upstream_unavailable",
]);
type Props = { id: string; error: unknown; locale: string; value: string; onChange: (value: string) => void; disabled: boolean };

export function RubricSaveModelRetry(props: Props) {
  return MODEL_ERRORS.has(getAPIErrorCode(props.error) ?? "") ? <ModelSelection {...props} /> : null;
}
function ModelSelection({ id, locale, value, onChange, disabled }: Props) {
  const providers = useStageProviders();
  const zh = locale === "zh-CN";
  const models = (providers.data ?? []).filter(model => model.enabled && model.provider_kind !== "ocr");
  return <div className="my-3 space-y-2">
    <StageProviderSelect id={id} locale={zh ? "zh-CN" : "en-US"} experts={models} value={value} onChange={onChange}
      disabled={disabled || providers.isPending} placeholder={zh ? "选择模型" : "Choose a model"}
      label={zh ? "换模型重试" : "Retry with another model"}
      hint={zh ? "选择模型后再次保存，当前修改已保留。" : "Choose a model and save again. Your edits are kept."} />
    {providers.isError ? <button type="button" onClick={() => void providers.refetch()} className="text-xs text-primary underline">{zh ? "重新加载模型" : "Reload models"}</button> : null}
    <Link to="/settings/byok" className="text-xs text-primary underline">{zh ? "API 设置" : "API settings"}</Link>
  </div>;
}

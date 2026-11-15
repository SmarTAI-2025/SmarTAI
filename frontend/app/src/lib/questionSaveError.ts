import { getAPIErrorCode } from "@/api/client";
const tx = (locale: string, zh: string, en: string) => locale === "zh-CN" ? zh : en;

export function questionSaveError(error: unknown, locale: string) {
  switch (getAPIErrorCode(error)) {
    case "no_provider_configured":
    case "recognition_provider_not_enabled":
      return tx(locale, "请先在 API 设置中启用模型，解析新评分标准后再保存。", "Enable a model in API settings to interpret the updated scoring rules, then save again.");
    case "provider_auth_failed":
    case "provider_permission_denied":
    case "provider_credentials_unavailable":
    case "provider_credentials_required":
      return tx(locale, "请在 API 设置中更新模型密钥或权限，再保存。", "Update the model's API key or permissions in API settings, then save again.");
    case "provider_quota_exceeded":
    case "provider_zero_quota":
    case "provider_daily_quota_exceeded":
    case "shared_pool_daily_limit_reached":
      return tx(locale, "请更换可用模型，或等模型额度恢复后再保存。", "Choose another available model, or save again once the model's quota is restored.");
    case "provider_model_not_found":
    case "provider_model_or_endpoint_not_found":
      return tx(locale, "请在 API 设置中核对模型名称和地址，再保存。", "Check the model name and endpoint in API settings, then save again.");
    case "invalid_alternative_rule":
      return tx(locale, "请核对其他解法的上下限与扣分规则；分数须在 0 与该小问满分之间。", "Check the alternative-method limits and deductions. Scores must be between 0 and this part's maximum.");
    case "invalid_structured_rubric":
      return tx(locale, "请核对评分标准中的分值、条件和对应小问，再保存。", "Check the points, conditions and matching question parts, then save again.");
    case "rubric_subpart_points_mismatch":
    case "rubric_subpart_points_missing":
      return tx(locale, "请补全各小问配分，并使合计等于本题满分。", "Complete the points for each part and make their sum equal this question's maximum.");
    case "version_conflict":
    case "stale_revision":
      return tx(locale, "题目已有新版本。请先复制当前修改，刷新核对后再保存。", "A newer version is available. Copy your edits, refresh to review it, then save again.");
    default:
      return tx(locale, "请稍后重试保存。", "Please try saving again shortly.");
  }
}

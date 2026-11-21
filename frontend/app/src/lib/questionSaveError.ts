import { getAPIErrorCode } from "@/api/client";
const tx = (locale: string, zh: string, en: string) => locale === "zh-CN" ? zh : en;

export function questionSaveError(error: unknown, locale: string) {
  return questionSaveErrorCode(getAPIErrorCode(error), locale);
}

export function questionSaveErrorCode(code: string | null | undefined, locale: string) {
  switch (code) {
    case "question_stem_required":
      return tx(locale, "请输入题干后再保存。", "Enter the question before saving.");
    case "question_stem_has_other_edits":
      return tx(locale, "请先处理本题其他未保存的修改，再更新题干。", "Resolve the other unsaved edits to this question before updating the question text.");
    case "question_preparation_wall_time_limit_reached":
    case "provider_timeout":
      return tx(locale, "更新标答和评分标准超时，请重试保存。", "Updating the answer and rubric timed out. Try saving again.");
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
    case "shared_pool_sol_daily_limit_reached":
    case "shared_pool_weekly_limit_reached":
      return tx(locale, "请更换可用模型，或等模型额度恢复后再保存。", "Choose another available model, or save again once the model's quota is restored.");
    case "shared_pool_budget_pending":
      return tx(locale, "共享调用正在结算，请稍后再保存。", "Shared calls are settling. Please save again shortly.");
    case "shared_pool_usage_unavailable":
    case "shared_pool_billing_unavailable":
      return tx(locale, "共享模型计费暂不可用，请联系管理员或选择自己的 BYOK 模型。", "Shared billing is unavailable. Contact an administrator or select your own BYOK model.");
    case "shared_pool_request_too_large":
      return tx(locale, "输入超出共享模型单次限制，请减少内容或选择自己的 BYOK 模型。", "Input exceeds the shared request limit. Reduce it or select your own BYOK model.");
    case "provider_model_not_found":
    case "provider_model_or_endpoint_not_found":
      return tx(locale, "请在 API 设置中核对模型名称和地址，再保存。", "Check the model name and endpoint in API settings, then save again.");
    case "invalid_alternative_rule":
      return tx(locale, "请核对其他解法的上下限与扣分规则；分数须在 0 与该小问满分之间。", "Check the alternative-method limits and deductions. Scores must be between 0 and this part's maximum.");
    case "programming_test_award_required":
      return tx(locale, "请先在「测试配分」填写分值。填 0 表示测试不单独计分。", "Fill in Test points first. Enter 0 for no separate test award.");
    case "invalid_programming_test_award":
      return tx(locale, "请在「测试配分」填写测试分，并在代码评分规则中分配其余分数。", "Enter Test points separately and allocate the remaining points in the code rubric.");
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

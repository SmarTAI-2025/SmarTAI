import type { Locale } from "@/i18n/messages";
import type { SubmissionPageError, SubmissionSourceOutcome } from "@/types";

export interface SubmissionSourceReasonCopy {
  title: string;
  description: string;
  nextStep?: string;
}

export function getSubmissionSourceReasonCopy(
  source: SubmissionSourceOutcome,
  locale: Locale,
): SubmissionSourceReasonCopy {
  if (source.resolution_status === "manual_resolved") return {
    title: tx(locale, "教师已补齐并确认", "Completed and confirmed by the teacher"),
    description: tx(locale, "原文件和识别记录已保留。", "Original files and recognition records are retained."),
  };
  if (source.status === "manual_review") return {
    title: tx(locale, "识别未完成，等待人工处理", "Recognition incomplete; awaiting manual review"),
    description: tx(locale, "原文件和已完成识别已保留。请逐题补齐作答或确认未作答，处理完成前不能批改。", "Originals and completed recognition are preserved. Complete each answer or confirm it is blank before grading."),
  };
  const copy = REASON_COPY[source.reason_code ?? source.internal_status];
  if (copy) return copy(locale, source);

  if (source.status === "processing") {
    return {
      title: tx(locale, "等待处理", "Waiting to be processed"),
      description: tx(locale, "原文件已保存，正在等待读取或识别。", "The original file is saved and waiting for extraction or recognition."),
    };
  }
  if (source.status === "parsed") {
    return parsedCopy(locale, source);
  }
  return {
    title: tx(locale, "这份作答未完成识别", "This submission was not recognized"),
    description: tx(
      locale,
      "未能识别作答，请重试。若仍失败，请联系管理员。",
      "Unable to recognize this submission. Try again or contact your administrator if it continues.",
    ),
  };
}

type ReasonFactory = (locale: Locale, source: SubmissionSourceOutcome) => SubmissionSourceReasonCopy;

export function getSubmissionPageErrorLabel(source: SubmissionSourceOutcome, error: SubmissionPageError, locale: Locale): string {
  const reason = REASON_COPY[error.error_code]?.(locale, source).title
    ?? tx(locale, "该页未识别成功，请重试", "This page was not recognized. Please retry");
  const status = typeof error.status_code === "number" && Number.isInteger(error.status_code)
    && error.status_code >= 100 && error.status_code <= 599 ? ` (HTTP ${error.status_code})` : "";
  return tx(locale, `第 ${error.page} 页：${reason}${status}`, `Page ${error.page}: ${reason}${status}`);
}

const REASON_COPY: Record<string, ReasonFactory> = {
  operation_cancelled: (locale) => ({
    title: tx(locale, "已手动暂停，尚未完成识别", "Manually paused before recognition completed"),
    description: tx(locale, "原文件和已完成识别已保留。可以继续识别未完成部分，也可以人工继续处理。", "Originals and completed recognition are preserved. Resume the unfinished work or continue manually."),
  }),
  image_format_mismatch: recognitionFileCopy,
  image_input_too_large: recognitionLimitCopy,
  image_invalid: recognitionFileCopy,
  image_mode_unsupported: recognitionFileCopy,
  image_multiframe_unsupported: recognitionFileCopy,
  image_orientation_invalid: recognitionFileCopy,
  image_pixel_limit_exceeded: (locale) => ({
    title: tx(locale, "图片尺寸过大", "Image dimensions are too large"),
    description: tx(locale, "请降低分辨率，或将图片裁成几张后重新上传。", "Reduce the resolution or crop the image into smaller images, then upload again."),
  }),
  image_processing_failed: recognitionProcessingCopy,
  image_response_too_large: recognitionLimitCopy,
  material_ocr_confirmation_required: recognitionOptInCopy,
  material_parser_provider_required: recognitionProviderCopy,
  material_recognition_review_required: recognitionReviewCopy,
  pdf_encrypted: recognitionFileCopy,
  pdf_evidence_busy: (locale) => ({
    title: tx(locale, "服务器正忙", "The server is busy"),
    description: tx(locale, "请稍后重试。", "Please try again later."),
  }),
  pdf_evidence_protocol_invalid: recognitionProcessingCopy,
  pdf_evidence_timeout: (locale) => ({
    title: tx(locale, "文件读取超时", "File reading timed out"),
    description: tx(locale, "请重试。", "Please try again."),
  }),
  pdf_input_too_large: recognitionLimitCopy,
  pdf_invalid: recognitionFileCopy,
  pdf_invalid_request: recognitionFileCopy,
  pdf_page_out_of_range: recognitionFileCopy,
  pdf_processing_failed: recognitionProcessingCopy,
  pdf_render_limit_exceeded: recognitionLimitCopy,
  pdf_response_too_large: recognitionLimitCopy,
  pdf_structure_limit_exceeded: recognitionLimitCopy,
  provider_credentials_required: recognitionProviderCopy,
  shared_pool_daily_limit_reached: (locale) => ({
    title: tx(locale, "共享模型日额度已用完。", "Shared model daily allowance exhausted."),
    description: tx(locale, "共享模型今日额度不足，本次识别请求未发送。", "The shared model daily allowance is insufficient. This recognition request was not sent."),
    nextStep: tx(locale, "请等待 UTC 00:00 重置、联系管理员调整，或在模型与 BYOK 页面配置自己的模型。", "Wait for UTC midnight, contact an administrator, or use your own BYOK model."),
  }),
  shared_pool_disabled: (locale) => ({
    title: tx(locale, "平台共享模型当前关闭。", "The shared model pool is disabled."),
    description: tx(locale, "平台共享模型当前不可用，本次识别请求未发送。", "The shared model pool is currently unavailable. This recognition request was not sent."),
    nextStep: tx(locale, "请在模型与 BYOK 页面配置自己的模型。", "Configure your own BYOK model."),
  }),
  provider_capacity_unavailable: (locale) => ({
    title: tx(locale, "等待调用资源超时", "Timed out waiting for model capacity"),
    description: tx(locale,
      "SmarTAI 未能在限定时间内取得调用名额，尚未向模型发送本次请求。任务资料已保留。",
      "SmarTAI could not obtain capacity before the deadline; this request was not sent to the model. Your task data is preserved."),
    nextStep: tx(locale,
      "请稍后重试；持续出现时请联系管理员检查服务器资源。",
      "Retry later; if this persists, ask an administrator to check server resources."),
  }),
  question_source_pages_out_of_range: (locale) => ({
    title: tx(locale, "请调整识别范围", "Adjust the recognition scope"),
    description: tx(locale, "填写的页码超出了 PDF 总页数，这份资料尚未调用识别模型。", "A selected page is outside this PDF. No recognition model was called for this source."),
    nextStep: tx(locale, "使用文件中的页序号，而非书上印刷的页码；页码留空则读取全部页面。", "Use file page positions rather than printed page numbers, or leave pages blank to read the full document."),
  }),
  question_source_incomplete: (locale) => ({
    title: tx(locale, "资料尚未完整读取", "Source reading is incomplete"),
    description: tx(locale, "部分页面尚未读完，已识别内容已保留。", "Some pages could not be read. Recognized content is retained."),
    nextStep: tx(locale, "检查资料识别状态后继续；文件与模型设置未变时会复用可用结果，结果未知的模型请求需要确认后才能重新提交。", "Check the source recognition status before continuing. Eligible results are reused if the file and model settings are unchanged; model requests with unknown outcomes need confirmation before resubmission."),
  }),
  recognition_already_running: recognitionStateCopy,
  recognition_artifact_invalid: recognitionStorageCopy,
  recognition_artifact_limit: recognitionLimitCopy,
  recognition_artifact_scope_unsupported: recognitionStateCopy,
  recognition_artifact_unavailable: recognitionStorageCopy,
  recognition_budget_exhausted: recognitionLimitCopy,
  recognition_cache_corrupt: recognitionStorageCopy,
  recognition_cache_not_success: recognitionStateCopy,
  recognition_cache_source_unavailable: recognitionStorageCopy,
  recognition_cache_unavailable: recognitionStorageCopy,
  recognition_input_unsupported: recognitionFileCopy,
  recognition_plan_changed: recognitionStateCopy,
  recognition_request_invalid: recognitionStateCopy,
  recognition_response_invalid: recognitionReviewCopy,
  recognition_response_too_large: recognitionLimitCopy,
  recognition_route_changed: recognitionStateCopy,
  recognition_source_mismatch: recognitionStorageCopy,
  recognition_source_unavailable: recognitionStorageCopy,
  recognition_timeout: (locale) => ({
    title: tx(locale, "识别超时", "Recognition timed out"),
    description: tx(locale, "请查看识别结果，重试未完成的部分。", "Check the recognition results and retry the unfinished parts."),
  }),
  target_location_needs_hint: recognitionScopeCopy,
  target_selection_limit_exceeded: recognitionScopeCopy,
  visual_capability_unavailable: recognitionProviderCopy,
  workflow_failed: (locale) => ({
    title: tx(locale, "识别任务意外终止", "The recognition task stopped unexpectedly"),
    description: tx(locale, "原文件和已完成的识别结果已保留。", "Your originals and completed recognition results are saved."),
    nextStep: tx(locale, "请重试；若仍失败，请联系管理员。", "Try again. If it still fails, contact your administrator."),
  }),
  no_provider_configured: (locale) => ({
    title: tx(locale, "尚未配置作答识别模型", "No submission-recognition model is configured"),
    description: tx(locale, "请先选择已启用的识别模型。", "Select an enabled recognition model."),
    nextStep: tx(locale, "先在模型设置中启用并选择一个识别模型，再重试这批原文件。", "Enable and select a recognition model in model settings, then retry these originals."),
  }),
  provider_not_enabled: (locale) => ({
    title: tx(locale, "所选模型已停用", "The selected model is disabled"),
    description: tx(locale, "识别未完成，原文件已保留。", "Recognition is incomplete. Your originals are saved."),
    nextStep: tx(locale, "重新启用该模型，或改选另一个已启用模型后重试。", "Re-enable this model or select another enabled model, then retry."),
  }),
  recognition_provider_not_enabled: (locale) => ({
    title: tx(locale, "作答识别模型已停用", "The submission-recognition model is disabled"),
    description: tx(locale, "识别未完成，原文件已保留。", "Recognition is incomplete. Your originals are saved."),
    nextStep: tx(locale, "在任务设置中改选已启用且与文件类型匹配的模型后重试。", "Choose an enabled model that supports the file type in task settings, then retry."),
  }),
  vision_provider_required: (locale) => ({
    title: tx(locale, "尚未选择可用的视觉模型", "No usable vision model is selected"),
    description: tx(
      locale,
      "原文件已保存，请选择支持图片识别的模型。",
      "The original file is saved. Select a model that supports image recognition.",
    ),
    nextStep: tx(locale, "添加或启用支持图片输入的视觉模型后重试，或上传可复制文字版 PDF/TXT。", "Add or enable a vision-capable model and retry, or upload a text-based PDF/TXT file."),
  }),
  provider_vision_not_supported: (locale) => ({
    title: tx(locale, "所选模型不支持图片输入", "The selected model does not support image input"),
    description: tx(locale, "这份文件需要图片识别，原文件已保存。", "This file needs image recognition. The original is saved."),
    nextStep: tx(locale, "在作答上传页改选支持视觉的模型，直接复用原文件重试。", "Choose a vision-capable model on the submission upload page and retry using the preserved original."),
  }),
  image_recognition_unconfirmed: (locale) => ({
    title: tx(locale, "图片识别尚未得到可靠内容", "Image recognition did not yield reliable content"),
    nextStep: tx(locale, "返回上传页换清晰文件或换模型后继续。", "Return to upload and try a clearer file or another model."),
    description: tx(locale, "请检查图片是否清晰、完整。也可在模型配置页验证图片识别能力。", "Check that the image is clear and complete. You can also verify image support in model settings."),
  }),
  ocr_empty_result: (locale) => ({
    title: tx(locale, "OCR 没有读到可用文字", "OCR found no usable text"),
    description: tx(
      locale,
      "请检查图片是否清晰、方向正确、内容完整。",
      "Check that the image is clear, correctly oriented, and complete.",
    ),
    nextStep: tx(locale, "检查清晰度和方向后重新拍摄；也可以换一个视觉模型重试。", "Check clarity and orientation, rescan the page, or retry with another vision model."),
  }),
  provider_timeout: (locale, source) => providerCopy(locale, source, "模型响应超时", "The model timed out", "请稍后重试。", "Please try again later."),
  provider_unreachable: (locale, source) => providerCopy(locale, source, "暂时无法连接模型服务", "The model service is unreachable", "检查网络或模型服务状态后重试。", "Check network and provider status, then retry."),
  provider_rate_limited: (locale, source) => providerCopy(locale, source, "模型服务仍返回限流", "The model service still reports a limit", "请查看服务商用量，稍后重试失败项，或更换模型。", "Check provider usage, retry failed items later, or change models."),
  provider_daily_quota_exceeded: (locale, source) => providerCopy(locale, source, "模型日额度已用完", "The model's daily quota is exhausted", "本轮已停止。请查看服务商用量，恢复后返回配置重新启动，或更换模型。", "This run has stopped. Check provider usage, then restart from settings or change models."),
  provider_overloaded: (locale, source) => providerCopy(locale, source, "模型服务当前拥堵", "The model service is busy", "原文件已保留。请稍后重试或选择另一个已启用模型。", "Your file is retained. Retry later or select another enabled model."),
  provider_auth_failed: (locale, source) => providerCopy(locale, source, "模型凭据无效或无权限", "The model credentials were rejected", "到 BYOK 检查 API Key、模型权限和余额后重试。", "Check the API key, model permission, and balance in BYOK, then retry."),
  provider_region_unsupported: (locale, source) => providerCopy(locale, source, "模型服务不支持当前网络地区", "The model service does not support the current network region", "请使用供应商支持的接入环境或另选已获授权的模型。", "Use a provider-supported access environment or another authorized model."),
  ocr_credential_not_found: (locale, source) => providerCopy(locale, source, "所选 OCR 凭据已不存在", "The selected OCR credential no longer exists", "返回 BYOK 保存百度 OCR 的 API Key / Secret Key，并重新选择识别服务。", "Save the Baidu OCR API Key / Secret Key in BYOK and select the recognition service again."),
  provider_permission_denied: (locale, source) => providerCopy(locale, source, "OCR 账号没有服务权限", "The OCR account lacks service permission", "在百度控制台确认已开通文档解析权限，或更换有权限的 API Key / Secret Key。", "Confirm Document Parsing access in the Baidu console or replace the API Key / Secret Key with an authorized pair."),
  provider_quota_exceeded: (locale, source) => providerCopy(locale, source, "服务商余额或额度已用完", "Provider credits or quota exhausted", "在对应服务商控制台检查余额与额度；恢复后重试，或切换模型。", "Check credits and quota in the provider console, then retry after recovery or switch models."),
  provider_unavailable: (locale, source) => providerCopy(locale, source, "OCR 服务暂时不可用", "The OCR service is temporarily unavailable", "请稍后重试。", "Please try again later."),
  provider_submit_uncertain: (locale, source) => providerCopy(locale, source, "识别未完成，重试需确认", "Recognition did not complete; confirm before retrying", "上次调用是否完成尚不确定，再次调用可能重复消耗额度。", "It is unclear whether the previous call completed. Calling again may use quota twice."),
  provider_task_failed: (locale, source) => providerCopy(locale, source, "OCR 服务端任务失败", "The provider-side OCR task failed", "检查文件格式和百度服务状态后重试。", "Check the file format and Baidu service status, then retry."),
  provider_request_failed: (locale, source) => providerCopy(locale, source, "OCR 请求失败", "The OCR request failed", "检查模型配置、网络与服务状态后重试。", "Check model settings, your network, and provider status, then retry."),
  provider_download_url_rejected: (locale, source) => providerCopy(locale, source, "OCR 结果下载地址未通过安全校验", "The OCR result URL failed security validation", "暂时无法获取识别结果，请联系管理员。", "Unable to retrieve recognition results. Contact your administrator."),
  provider_result_unavailable: (locale, source) => providerCopy(locale, source, "OCR 结果暂时不可用", "The OCR result is unavailable", "暂时无法获取识别结果，请稍后查看。", "Recognition results are not available yet. Please check again later."),
  provider_result_too_large: (locale, source) => providerCopy(locale, source, "OCR 结果超过安全上限", "The OCR result exceeds the safety limit", "识别结果过大，请拆分文件后重试。", "The recognition result is too large. Split the file and try again."),
  provider_model_not_found: (locale, source) => providerCopy(locale, source, "模型名称不存在或无权限", "The model was not found or is unavailable", "核对模型名称，或改选另一个已启用模型后重试。", "Check the model name or retry with another enabled model."),
  provider_credentials_unavailable: (locale, source) => providerCopy(locale, source, "无法读取已保存的模型凭据", "Saved model credentials could not be loaded", "重新保存 BYOK 凭据后重试。", "Save the BYOK credentials again, then retry."),
  provider_model_or_endpoint_not_found: (locale, source) => providerCopy(locale, source, "模型名称或接口路径不存在", "The model or endpoint was not found", "在 BYOK 核对模型名称与 Base URL；中转站只填写服务根路径，不要填写完整操作终点。", "Check the model name and base URL in BYOK. Enter the relay service root, not a full operation endpoint."),
  provider_request_rejected: (locale, source) => providerCopy(locale, source, "模型接口未接受本次请求", "The model API did not accept this request", "核对模型名称、接口地址和 API 协议，或更换模型后重试。", "Check the model name, endpoint, and API protocol, or retry with another model."),
  provider_recitation_blocked: (locale, source) => providerCopy(locale, source, "模型停止了原文转写", "The model stopped transcribing this content", "模型因版权限制停止转写，请更换识别模型后重试。", "The model stopped transcription due to copyright restrictions. Change the recognition model and retry."),
  provider_content_blocked: (locale, source) => providerCopy(locale, source, "模型限制了这次内容输出", "The model blocked this content output", "模型因内容限制停止输出，请更换模型后重试。", "The model stopped output due to content restrictions. Change models and retry."),
  provider_upstream_unavailable: (locale, source) => providerCopy(locale, source, "模型服务暂时不可用", "The model service is temporarily unavailable", "稍后重试，或换用另一个已启用模型。", "Retry later or choose another enabled model."),
  provider_response_invalid: (locale, source) => providerCopy(locale, source, "模型返回格式无法解析", "The model returned an invalid structure", "重试本次识别，或更换模型；原文件已保留。", "Retry recognition or change models; the original file is preserved."),
  provider_image_payload_invalid: (locale, source) => providerCopy(locale, source, "图片无法读取", "The image could not be read", "重新上传清晰的 JPG、PNG 或 WebP；若仍失败，请联系管理员。", "Upload a clear JPG, PNG, or WebP again. If it still fails, contact your administrator."),
  provider_message_payload_not_supported: (locale, source) => providerCopy(locale, source, "当前协议无法发送这类输入", "The selected protocol cannot encode this input", "核对高级 API 协议，或改用支持当前文件类型的模型。", "Check the Advanced API protocol or choose a model that supports this file type."),
  provider_endpoint_dns_failed: (locale, source) => providerCopy(locale, source, "中转站域名无法解析", "The relay hostname could not be resolved", "检查 Base URL 拼写和中转站服务状态后重试。", "Check the base URL spelling and relay status, then retry."),
  provider_endpoint_non_public_address: (locale, source) => providerCopy(locale, source, "中转站域名解析到非公网地址", "The relay hostname resolved to a non-public address", "检查中转站 DNS 配置后重试；系统已阻止访问内网或特殊地址。", "Check the relay DNS configuration, then retry. Access to private or special addresses was blocked."),
  provider_endpoint_tls_failed: (locale, source) => providerCopy(locale, source, "中转站 HTTPS 证书校验失败", "The relay HTTPS certificate could not be verified", "请让服务提供方修复证书后重试。", "Ask the service provider to fix its certificate before retrying."),
  provider_endpoint_redirect_blocked: (locale, source) => providerCopy(locale, source, "中转站返回了重定向", "The relay returned a redirect", "请将 Base URL 改为最终的 HTTPS 服务地址。", "Set the Base URL to the final HTTPS service address."),
  provider_endpoint_protocol_mismatch: (locale, source) => providerCopy(locale, source, "中转站响应与所选 API 协议不一致", "The relay response does not match the selected API protocol", "在 BYOK 的高级设置中核对 OpenAI、Anthropic 或 Gemini 协议。", "Check the OpenAI, Anthropic, or Gemini protocol in BYOK Advanced settings."),
  provider_endpoint_response_too_large: (locale, source) => providerCopy(locale, source, "中转站响应超过安全大小上限", "The relay response exceeded the safety limit", "缩小输入或输出范围后重试；若持续出现，请联系管理员评估限制。", "Reduce the input or requested output and retry. If it persists, ask an administrator to review the limit."),
  submission_parse_invalid: (locale) => ({
    title: tx(locale, "模型返回格式无法解析", "The model returned an invalid structure"),
    description: tx(
      locale,
      "未能完整识别学生信息与作答，请重试或更换模型。",
      "Could not recognize all student details and answers. Retry or choose another model.",
    ),
    nextStep: tx(locale, "可直接重试；若持续出现，换用结构化输出更稳定的模型。", "Retry once; if it persists, choose a model with more reliable structured output."),
  }),
  submission_parse_failed: (locale) => ({
    title: tx(locale, "作答识别没有完成", "Submission recognition did not complete"),
    description: tx(
      locale,
      "未能识别作答，请重试；若仍失败，请联系管理员。",
      "Unable to recognize the submission. Retry or contact your administrator if it continues.",
    ),
    nextStep: tx(locale, "若重试后仍失败，请联系管理员。", "If another attempt fails, contact your administrator."),
  }),
  submission_model_field_too_long: (locale) => ({
    title: tx(locale, "识别结果格式有误", "The recognition result has an invalid format"),
    description: tx(
      locale,
      "本份文件的识别结果格式有误，请重试或更换模型。",
      "This file’s recognition result has an invalid format. Retry or choose another model.",
    ),
    nextStep: tx(locale, "请重试；若仍失败，请更换模型并检查文件排版。", "Retry; if it continues, change models and check the file layout."),
  }),
  submission_source_persistence_failed: (locale) => ({
    title: tx(locale, "原文件保存未完成", "The original file was not saved"),
    description: tx(
      locale,
      "原文件未能保存，请重新上传。",
      "The original file could not be saved. Please upload it again.",
    ),
    nextStep: tx(locale, "若重新上传后仍失败，请联系管理员。", "If another upload fails, contact your administrator."),
  }),
  submission_persistence_failed: (locale) => ({
    title: tx(locale, "识别结果写入任务失败", "Recognized results could not be saved to the task"),
    description: tx(
      locale,
      "作答结果未能保存，请联系管理员。",
      "The recognized answers could not be saved. Contact your administrator.",
    ),
    nextStep: tx(locale, "请联系管理员处理后重试。", "Contact your administrator, then retry once the issue is resolved."),
  }),
  submission_outcome_persistence_failed: (locale) => ({
    title: tx(locale, "逐文件识别结果保存未完成", "Per-file outcomes were not fully saved"),
    description: tx(
      locale,
      "部分文件的识别结果未能保存，请联系管理员。",
      "Recognition results for some files could not be saved. Contact your administrator.",
    ),
    nextStep: tx(locale, "原文件已保留，请联系管理员处理后重试。", "Your originals are saved. Contact your administrator, then retry once the issue is resolved."),
  }),
  no_answer_content_detected: (locale) => ({
    title: tx(locale, "没有提取到任何作答内容", "No answer content was detected"),
    description: tx(
      locale,
      "未找到作答内容。请检查是否上传了空白页、封面或不完整的答案区域。",
      "No answers were found. Check for blank pages, cover pages, or cropped answer areas.",
    ),
    nextStep: tx(locale, "打开原文件确认答案区域存在且完整；必要时重新拍摄，或换一个识别模型重试。", "Open the original and confirm the answer area is present and complete; rescan or retry with another recognition model if needed."),
  }),
  no_matching_answer: (locale, source) => ({
    title: tx(locale, "没有作答能对应当前任务题目", "No answers match this task's questions"),
    description: tx(
      locale,
      "已识别作答，但题号与当前任务不匹配。请核对文件和题号。",
      "Answers were recognized, but their question numbers do not match this task. Check the file and question numbers.",
    ),
    nextStep: source.unknown_question_ids.length
      ? tx(locale, `识别到的未匹配题号：${source.unknown_question_ids.join("、")}。请核对原文件与当前题目。`, `Unmatched IDs found: ${source.unknown_question_ids.join(", ")}. Compare the original file with the current questions.`)
      : tx(locale, "打开原文件核对是否属于本次作业，并检查题号是否清晰。", "Open the original and verify that it belongs to this assignment and has readable question labels."),
  }),
  duplicate_student_identity: (locale, source) => ({
    title: tx(locale, "多份文件识别成同一位学生", "Multiple files resolve to the same student"),
    description: tx(
      locale,
      `至少两份原文件都识别为${source.student_candidate ? `“${source.student_candidate}”` : "同一身份"}。系统保留了每一份文件，没有用后一份覆盖前一份。`,
      `At least two originals resolved to ${source.student_candidate ? `“${source.student_candidate}”` : "the same identity"}. Every file was preserved; later files did not overwrite earlier ones.`,
    ),
    nextStep: tx(locale, "逐份核对姓名/学号，为错误的一份修正身份后再继续。", "Review each file and correct the student identity before continuing."),
  }),
  identity_needs_review: (locale, source) => ({
    title: tx(locale, "学生身份需要教师确认", "Student identity needs teacher review"),
    description: tx(
      locale,
      source.student_candidate
        ? `系统提取到候选身份“${source.student_candidate}”，但无法唯一确认。作答内容已保留。`
        : "系统读取到了作答，但无法从文件名、正文或名单中唯一确认学生身份。",
      source.student_candidate
        ? `The system found candidate “${source.student_candidate}” but could not confirm it uniquely. The answers are preserved.`
        : "The answers were read, but the student could not be uniquely confirmed from the filename, content, or roster.",
    ),
    nextStep: tx(locale, "核对原文件并确认或修正学号和姓名。", "Compare the original and confirm or correct the student ID and name."),
  }),
  student_identity_conflict: (locale, source) => ({
    title: tx(locale, "学生身份发生冲突", "The student identity conflicts"),
    description: tx(
      locale,
      source.student_candidate
        ? `候选身份“${source.student_candidate}”与当前任务中的其他身份记录冲突，作答和原文件均已保留。`
        : "系统读取到了作答，但身份记录与当前任务中的其他记录冲突。",
      source.student_candidate
        ? `Candidate “${source.student_candidate}” conflicts with another identity in this task. The answers and original are preserved.`
        : "The answers were read, but the identity conflicts with another record in this task.",
    ),
    nextStep: tx(locale, "逐份核对原文件并确认正确学号和姓名。", "Review the originals and confirm the correct student ID and name."),
  }),
  source_decode_failed: (locale) => fileCopy(locale, "文本编码无法读取", "The text encoding could not be read", "请把文本另存为 UTF-8，或上传 PDF/图片版本。", "Save the text as UTF-8, or upload a PDF/image version."),
  submission_source_content_type_mismatch: (locale) => fileCopy(locale, "文件内容与扩展名或类型不一致", "The file contents do not match its name or declared type", "不要只修改扩展名；请从原应用重新导出为受支持的 PDF、TXT、JPG、PNG 或 WebP 后上传。", "Do not only rename the extension. Export the file again as a supported PDF, TXT, JPG, PNG, or WebP, then upload it."),
  submission_source_empty: (locale) => fileCopy(locale, "文件没有可识别内容", "The file contains no recognizable content", "检查是否为空白页、空文件或只包含不可见字符。", "Check for a blank page, empty file, or invisible-only content."),
  submission_source_unsupported: (locale) => fileCopy(locale, "文件类型暂不支持", "This file type is not supported", "转换为 PDF、TXT、Markdown、CSV、JPG、PNG 或 WebP 后重新上传。", "Convert it to PDF, TXT, Markdown, CSV, JPG, PNG, or WebP and upload again."),
  submission_source_too_large: (locale) => fileCopy(locale, "文件超过处理上限", "The file exceeds the processing limit", "压缩图片，或拆分 PDF/压缩包后重新上传。", "Compress the image or split the PDF/archive, then upload again."),
  ocr_input_invalid: (locale) => fileCopy(locale, "文件内容不是有效的 OCR 输入", "The file is not valid OCR input", "从原应用重新导出文件，不要只修改扩展名。", "Export the file again from its source application; do not only rename the extension."),
  ocr_unsupported_file: (locale) => fileCopy(locale, "百度 OCR 不支持该文件格式", "Baidu OCR does not support this file format", "转换为 PDF、JPG、PNG、BMP 或 TIFF，或更换识别模型。", "Convert it to PDF, JPG, PNG, BMP, or TIFF, or choose another recognition model."),
  ocr_file_too_large: (locale) => fileCopy(locale, "文件超过百度 OCR 上传上限", "The file exceeds the Baidu OCR upload limit", "压缩图片或拆分文档后重新上传。", "Compress the image or split the document, then upload again."),
  ocr_image_dimension_limit_exceeded: (locale) => fileCopy(locale, "图片边长超过 8192 像素", "An image edge exceeds 8192 pixels", "缩小图片尺寸并保持文字清晰后重新上传。", "Resize the image while keeping the text clear, then upload it again."),
  media_inspection_unavailable: (locale) => fileCopy(locale, "文件安全检查暂时不可用", "Media safety inspection is unavailable", "请稍后重试，或联系管理员。", "Try again later or contact your administrator."),
  media_inspection_busy: (locale) => fileCopy(locale, "文件安全检查正忙", "Media safety inspection is busy", "稍等片刻后重试；文件尚未发送给百度。", "Wait briefly and retry. The file has not been sent to Baidu."),
  media_inspection_timeout: (locale) => fileCopy(locale, "文件安全检查超时", "Media safety inspection timed out", "优化或拆分文件后重试；文件尚未发送给百度。", "Optimize or split the file, then retry. The file was not sent to Baidu."),
  media_inspection_failed: (locale) => fileCopy(locale, "文件未通过安全检查", "Media safety inspection failed", "检查文件是否损坏或加密，并重新导出。", "Check whether the file is damaged or encrypted, then export it again."),
  pdf_processing_unavailable: (locale) => fileCopy(locale, "服务器暂时不能处理 PDF", "PDF processing is unavailable on the server", "请稍后重试，或将文件转为图片或 TXT 后上传。", "Try again later, or upload the content as images or TXT."),
  pdf_extraction_busy: (locale) => fileCopy(locale, "PDF 读取服务正忙", "PDF extraction is busy", "稍等片刻后直接重试；原文件已经保存。", "Wait briefly and retry; the original file is already saved."),
  pdf_extraction_timeout: (locale) => fileCopy(locale, "PDF 读取超时", "PDF extraction timed out", "拆分或优化 PDF 后重试。", "Split or optimize the PDF, then retry."),
  pdf_page_limit_exceeded: (locale) => fileCopy(locale, "PDF 页数超过上限", "The PDF exceeds the page limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_character_limit_exceeded: (locale) => fileCopy(locale, "PDF 文字量超过上限", "The PDF exceeds the text limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_extraction_failed: (locale) => fileCopy(locale, "PDF 内容无法读取", "The PDF could not be read", "检查文件是否损坏或加密，并尝试重新导出。", "Check whether the file is damaged or encrypted, and export it again."),
  pdf_ocr_render_failed: (locale) => fileCopy(locale, "扫描 PDF 无法转成 OCR 页面", "The scanned PDF could not be rendered for OCR", "尝试重新导出 PDF，或逐页转为清晰图片。", "Export the PDF again, or convert each page to a clear image."),
  submission_archive_empty: (locale) => archiveCopy(locale, "压缩包中没有学生文件", "The archive contains no student files", "确认压缩包不是空的，且文件没有全部放在系统隐藏目录中。", "Make sure the archive is not empty and files are not all in hidden system folders."),
  submission_archive_invalid: (locale) => archiveCopy(locale, "压缩包损坏或结构不安全", "The archive is damaged or unsafe", "请重新打包为未加密的 ZIP 后上传。", "Create a new, unencrypted ZIP and upload it again."),
  submission_archive_limit_exceeded: (locale) => archiveCopy(locale, "压缩包超过安全上限", "The archive exceeds a safety limit", "减少文件数量、单文件大小或解压后总体积，再重新上传。", "Reduce file count, member size, or total expanded size, then upload again."),
  submission_archive_member_too_large: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件过大", "This archive member is too large", "只压缩或拆分这份文件后重新打包；同包其他健康文件已经继续处理。", "Compress or split only this member, then rebuild the archive. Other healthy members continued processing."),
  submission_archive_member_unreadable: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件无法读取", "This archive member could not be read", "重新导出这份文件并重新打包；系统保留了整个原始压缩包供核对。", "Export this member again and rebuild the archive. The original container was preserved for review."),
  submission_archive_member_unsafe_path: (locale) => archiveMemberCopy(locale, "压缩包成员路径不安全", "This archive member has an unsafe path", "压缩包无法读取，请将学生文件重新打包为 ZIP 后上传。", "The archive could not be read. Repack the student files as a ZIP and upload it again."),
};

function parsedCopy(locale: Locale, source: SubmissionSourceOutcome): SubmissionSourceReasonCopy {
  return {
    title: source.unknown_question_ids.length
      ? tx(locale, "已识别，部分题号未匹配", "Recognized with some unmatched question IDs")
      : tx(locale, "识别成功", "Recognized"),
    description: tx(
      locale,
      `已对应 ${source.matched_answer_count} 道当前任务题目。`,
      `${source.matched_answer_count} answer(s) matched the current task.`,
    ),
    nextStep: source.unknown_question_ids.length
      ? tx(locale, `未匹配题号：${source.unknown_question_ids.join("、")}。`, `Unmatched IDs: ${source.unknown_question_ids.join(", ")}.`)
      : undefined,
  };
}

function providerCopy(locale: Locale, source: SubmissionSourceOutcome, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  const isOcr = source.failure_phase === "ocr";
  return {
    title: tx(locale, isOcr ? `OCR：${zhTitle}` : zhTitle, isOcr ? `OCR: ${enTitle}` : enTitle),
    description: tx(locale, isOcr ? "原文件已保存，但本次 OCR 模型调用没有得到可用结果。" : "原文件已保存，但本次模型调用没有得到可用结果。", isOcr ? "The original is saved, but this OCR model call produced no usable result." : "The original file is saved, but this model call produced no usable result."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function fileCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "原文件已保存，但读取阶段没有得到可供识别的正文。", "The original is saved, but the read stage produced no usable text."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function archiveCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "压缩包未处理完成，原文件已保留。", "The archive could not be fully processed. The original is saved."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function archiveMemberCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "这份文件未能处理，原始压缩包和其他文件已保留。", "This file could not be processed. The original archive and other files are retained."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

function recognitionFileCopy(locale: Locale): SubmissionSourceReasonCopy {
  return fileCopy(locale, "原文件格式或页码需要检查", "Check source format or page range", "核对文件是否损坏、加密或含多帧图片；重新导出 PDF/PNG，或修正页码后再上传。", "Check for damage, encryption or multi-frame images. Export PDF/PNG or correct the page range before uploading again.");
}

function recognitionLimitCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "达到本次识别资源上限", "Recognition resource limit reached"), description: tx(locale, "已达到处理上限，请缩小范围后重试。已识别内容已保留。", "The processing limit was reached. Reduce the scope and try again. Recognized content is retained."), nextStep: tx(locale, "缩小题目或页码范围，重试未完成部分。", "Narrow the question or page range and retry the unfinished parts.") };
}

function recognitionProcessingCopy(locale: Locale): SubmissionSourceReasonCopy {
  return fileCopy(locale, "本次文件处理未完成", "File processing did not complete", "请稍后重试读取；若仍失败，请核对原件或联系管理员。", "Please try reading the file again later. If it still fails, check the original or contact an administrator.");
}

function recognitionOptInCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "此资料尚未启用图像识别", "Image recognition is not enabled for this material"), description: tx(locale, "当前资料需要视觉读取，尚未提交模型调用。", "This material needs visual reading; no model request was submitted."), nextStep: tx(locale, "在资料上传设置中启用图片识别，或改用可复制文字版本。", "Enable image recognition in the upload settings or use a text-based version.") };
}

function recognitionProviderCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "需要匹配的识别或解析模型", "A compatible recognition or parsing model is required"), description: tx(locale, "请选择可用的识别模型后重试。", "Select an available recognition model and try again."), nextStep: tx(locale, "核对本阶段已选模型及凭据；专用 OCR 的结果需要另行选择结构化解析模型。", "Check the selected stage model and credentials. Dedicated OCR output needs a separately selected structured parser.") };
}

function recognitionReviewCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "识别结果需要核对", "Recognition needs review"), description: tx(locale, "部分内容未识别清楚或不完整。", "Some content is unclear or incomplete."), nextStep: tx(locale, "请对照原文件核对作答，并修正或确认。", "Compare the answers with the original, then correct or confirm them.") };
}

function recognitionStateCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "识别状态需要核实", "Verify recognition state"), description: tx(locale, "任务仍在处理，或资料、模型设置已变化。", "The task is still running, or its source or model settings have changed."), nextStep: tx(locale, "请先刷新任务状态；若仍无进展，请联系管理员核对后再重试。", "Refresh the task. If it remains stuck, ask your administrator to check before retrying.") };
}

function recognitionStorageCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "已保存的识别资料暂不可用", "Saved recognition data is unavailable"), description: tx(locale, "暂时无法读取原文件或已保存的识别结果。", "The original file or saved recognition results could not be read."), nextStep: tx(locale, "请刷新后重试读取；若仍失败，请联系管理员。", "Refresh and try reading again. If it still fails, contact your administrator.") };
}

function recognitionScopeCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "请缩小或明确识别范围", "Narrow or clarify the recognition scope"), description: tx(locale, "目标题未能可靠定位，或所选范围超过本次上限。", "Target questions were not reliably located or the selected scope exceeded this request's limit."), nextStep: tx(locale, "请补充题号和页码，再开始识别。", "Add question and page numbers, then start recognition.") };
}

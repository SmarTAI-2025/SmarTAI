import type { Locale } from "@/i18n/messages";
import type { SubmissionSourceOutcome } from "@/types";

export interface SubmissionSourceReasonCopy {
  title: string;
  description: string;
  nextStep?: string;
}

export function getSubmissionSourceReasonCopy(
  source: SubmissionSourceOutcome,
  locale: Locale,
): SubmissionSourceReasonCopy {
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
      "后端保留了原文件和诊断代码，但没有得到可用作答。可按下方错误代码重试；若重复出现，请把任务编号交给管理员排查。",
      "The backend preserved the original file and diagnostic code but produced no usable answers. Retry using the code below; if it repeats, share the job ID with an administrator.",
    ),
  };
}

type ReasonFactory = (locale: Locale, source: SubmissionSourceOutcome) => SubmissionSourceReasonCopy;

const REASON_COPY: Record<string, ReasonFactory> = {
  image_format_mismatch: recognitionFileCopy,
  image_input_too_large: recognitionLimitCopy,
  image_invalid: recognitionFileCopy,
  image_mode_unsupported: recognitionFileCopy,
  image_multiframe_unsupported: recognitionFileCopy,
  image_orientation_invalid: recognitionFileCopy,
  image_pixel_limit_exceeded: recognitionLimitCopy,
  image_processing_failed: recognitionProcessingCopy,
  image_response_too_large: recognitionLimitCopy,
  material_ocr_confirmation_required: recognitionOptInCopy,
  material_parser_provider_required: recognitionProviderCopy,
  material_recognition_review_required: recognitionReviewCopy,
  pdf_encrypted: recognitionFileCopy,
  pdf_evidence_busy: recognitionProcessingCopy,
  pdf_evidence_protocol_invalid: recognitionProcessingCopy,
  pdf_evidence_timeout: recognitionProcessingCopy,
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
  question_source_pages_out_of_range: (locale) => ({
    title: tx(locale, "请调整识别范围", "Adjust the recognition scope"),
    description: tx(locale, "填写的页码超出了 PDF 总页数，这份资料尚未调用识别模型。", "A selected page is outside this PDF. No recognition model was called for this source."),
    nextStep: tx(locale, "使用文件中的页序号，而非书上印刷的页码；页码留空则读取全部页面。", "Use file page positions rather than printed page numbers, or leave pages blank to read the full document."),
  }),
  question_source_incomplete: (locale) => ({
    title: tx(locale, "资料尚未完整读取", "Source reading is incomplete"),
    description: tx(locale, "仍有页面未成功读取，已暂停题目生成。已完成的页面证据已保存；不会把部分内容当作整份文件完成。", "Some pages could not be read, so question generation is paused. Completed page evidence is saved; partial input is not treated as a complete document."),
    nextStep: tx(locale, "检查资料识别状态后继续；已完成页面会复用，结果未知的模型请求需要确认后才能重新提交。", "Check the source recognition status before continuing. Completed pages are reused; model requests with unknown outcomes need confirmation before resubmission."),
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
  recognition_timeout: recognitionStateCopy,
  target_location_needs_hint: recognitionScopeCopy,
  target_selection_limit_exceeded: recognitionScopeCopy,
  visual_capability_unavailable: recognitionProviderCopy,
  workflow_failed: (locale) => ({
    title: tx(locale, "识别任务意外终止", "The recognition task stopped unexpectedly"),
    description: tx(locale, "系统已停止本次任务并保留原文件；没有把未确认的结果继续显示为处理中或送入批改。", "The run stopped and the originals were preserved. Unconfirmed results were not left processing or sent to grading."),
    nextStep: tx(locale, "先重试一次；若再次出现，请携带任务编号让管理员查看服务端日志。", "Retry once. If it happens again, share the job ID with an administrator to inspect server logs."),
  }),
  no_provider_configured: (locale) => ({
    title: tx(locale, "尚未配置作答识别模型", "No submission-recognition model is configured"),
    description: tx(locale, "系统没有可用于读取并结构化学生作答的已启用模型，因此没有开始猜测或生成结果。", "No enabled model is available to read and structure student submissions, so the system did not guess or generate results."),
    nextStep: tx(locale, "先在模型设置中启用并选择一个识别模型，再重试这批原文件。", "Enable and select a recognition model in model settings, then retry these originals."),
  }),
  provider_not_enabled: (locale) => ({
    title: tx(locale, "所选模型已停用", "The selected model is disabled"),
    description: tx(locale, "任务引用的模型配置仍然存在，但当前不可用；原文件没有因此被当成识别成功。", "The referenced model configuration still exists but is currently unavailable. The originals were not treated as successfully recognized."),
    nextStep: tx(locale, "重新启用该模型，或改选另一个已启用模型后重试。", "Re-enable this model or select another enabled model, then retry."),
  }),
  recognition_provider_not_enabled: (locale) => ({
    title: tx(locale, "作答识别模型已停用", "The submission-recognition model is disabled"),
    description: tx(locale, "本任务指定的作答识别模型当前不可用，因此 OCR/结构识别没有继续执行。", "The recognition model selected for this task is unavailable, so OCR and structured recognition did not continue."),
    nextStep: tx(locale, "在任务设置中改选已启用且与文件类型匹配的模型后重试。", "Choose an enabled model that supports the file type in task settings, then retry."),
  }),
  vision_provider_required: (locale) => ({
    title: tx(locale, "尚未选择可用的视觉模型", "No usable vision model is selected"),
    description: tx(
      locale,
      "这份文件需要读取图片或扫描页，但当前阶段没有可用的视觉模型。原文件已保存，并不是文件丢失或学生答案有误。",
      "This file requires image or scanned-page reading, but no usable vision model is selected for this stage. The original is saved; the file was not lost and the student's answer is not at fault.",
    ),
    nextStep: tx(locale, "添加或启用支持图片输入的视觉模型后重试，或上传可复制文字版 PDF/TXT。", "Add or enable a vision-capable model and retry, or upload a text-based PDF/TXT file."),
  }),
  provider_vision_not_supported: (locale) => ({
    title: tx(locale, "所选模型不支持图片输入", "The selected model does not support image input"),
    description: tx(locale, "这份文件需要读取图片或扫描页，但本阶段明确选择的模型拒绝了视觉输入。原文件已保存。", "This file requires image or scanned-page reading, but the model explicitly selected for this stage rejected visual input. The original is saved."),
    nextStep: tx(locale, "在作答上传页改选支持视觉的模型，直接复用原文件重试。", "Choose a vision-capable model on the submission upload page and retry using the preserved original."),
  }),
  image_recognition_unconfirmed: (locale) => ({
    title: tx(locale, "图片识别尚未得到可靠内容", "Image recognition did not yield reliable content"),
    nextStep: tx(locale, "返回上传页换清晰文件或换模型后继续。", "Return to upload and try a clearer file or another model."),
    description: tx(locale, "文件模糊或模型识别效果可能导致低质量；不能据此判定不支持图片。请换清晰文件或换模型；图片能力未知时可主动验证。", "Blurry input or model accuracy can cause poor results; this does not prove lack of image support. Try a clearer file or another model, or verify image capability if unknown."),
  }),
  ocr_empty_result: (locale) => ({
    title: tx(locale, "OCR 没有读到可用文字", "OCR found no usable text"),
    description: tx(
      locale,
      "模型完成了图片读取，但返回内容为空。常见原因是照片模糊、方向错误、反光、裁切不全或页面本身空白。",
      "The image request completed, but OCR returned no text. Common causes include blur, wrong orientation, glare, cropping, or a blank page.",
    ),
    nextStep: tx(locale, "检查清晰度和方向后重新拍摄；也可以换一个视觉模型重试。", "Check clarity and orientation, rescan the page, or retry with another vision model."),
  }),
  provider_timeout: (locale, source) => providerCopy(locale, source, "模型响应超时", "The model timed out", "稍后重试；原文件已经保存，不需要重新整理整批文件。", "Retry later. The original is saved, so the whole batch does not need to be rebuilt."),
  provider_unreachable: (locale, source) => providerCopy(locale, source, "暂时无法连接模型服务", "The model service is unreachable", "检查网络或模型服务状态后重试。", "Check network and provider status, then retry."),
  provider_rate_limited: (locale, source) => providerCopy(locale, source, "模型服务触发限流", "The model service rate-limited the request", "等待限额恢复后重试，或换用另一个已启用模型。", "Wait for the quota window or retry with another enabled model."),
  provider_overloaded: (locale, source) => providerCopy(locale, source, "模型服务当前拥堵", "The model service is busy", "原文件已保留。稍后重试或选择另一个已启用模型；这不是已确认的每日额度耗尽。", "Your file is preserved. Retry later or select another enabled model; a daily quota limit has not been confirmed."),
  provider_auth_failed: (locale, source) => providerCopy(locale, source, "模型凭据无效或无权限", "The model credentials were rejected", "到 BYOK 检查 API Key、模型权限和余额后重试。", "Check the API key, model permission, and balance in BYOK, then retry."),
  provider_region_unsupported: (locale, source) => providerCopy(locale, source, "模型服务不支持当前网络地区", "The model service does not support the current network region", "请使用供应商支持的接入环境或另选已获授权的模型。", "Use a provider-supported access environment or another authorized model."),
  ocr_credential_not_found: (locale, source) => providerCopy(locale, source, "所选 OCR 凭据已不存在", "The selected OCR credential no longer exists", "返回 BYOK 保存百度 OCR 的 AK/SK，并重新选择识别服务。", "Save the Baidu OCR AK/SK in BYOK and select the recognition service again."),
  provider_permission_denied: (locale, source) => providerCopy(locale, source, "OCR 账号没有服务权限", "The OCR account lacks service permission", "在百度控制台确认已开通文档解析权限，或更换有权限的 AK/SK。", "Confirm Document Parsing access in the Baidu console or replace the AK/SK with an authorized pair."),
  provider_quota_exceeded: (locale, source) => providerCopy(locale, source, "OCR 额度已用完", "The OCR quota is exhausted", "在百度控制台检查当前活动额度与用量；额度恢复后再重试。", "Check the current campaign quota and usage in the Baidu console, then retry after quota is available."),
  provider_unavailable: (locale, source) => providerCopy(locale, source, "OCR 服务暂时不可用", "The OCR service is temporarily unavailable", "稍后重试；系统不会静默换用其他服务。", "Retry later. SmarTAI will not silently switch to another service."),
  provider_submit_uncertain: (locale, source) => providerCopy(locale, source, "OCR 提交状态无法确认", "The OCR submission state is uncertain", "为避免重复计费，系统不会自动重提这份文件。请保留任务编号并让管理员核对后再决定下一步。", "To avoid duplicate billing, SmarTAI will not resubmit this file automatically. Keep the job ID and ask an administrator to verify the provider state first."),
  provider_task_failed: (locale, source) => providerCopy(locale, source, "OCR 服务端任务失败", "The provider-side OCR task failed", "检查文件格式和百度服务状态后，再由教师明确重试。", "Check the file format and Baidu service status, then retry explicitly."),
  provider_request_failed: (locale, source) => providerCopy(locale, source, "OCR 请求失败", "The OCR request failed", "检查 BYOK、网络与服务状态后重试；系统没有改用其他模型。", "Check BYOK, network, and provider status, then retry. No alternate model was used."),
  provider_download_url_rejected: (locale, source) => providerCopy(locale, source, "OCR 结果下载地址未通过安全校验", "The OCR result URL failed security validation", "请保留任务编号联系管理员核对服务商返回域名；不要重复提交原文件。", "Keep the job ID and ask an administrator to verify the provider result host. Do not resubmit the original."),
  provider_result_unavailable: (locale, source) => providerCopy(locale, source, "OCR 结果暂时不可用", "The OCR result is unavailable", "请保留任务编号并稍后查看；系统不会为取得结果而重复提交文件。", "Keep the job ID and check later. SmarTAI will not resubmit the file to obtain a result."),
  provider_result_too_large: (locale, source) => providerCopy(locale, source, "OCR 结果超过安全上限", "The OCR result exceeds the safety limit", "拆分文件后由教师明确重试；当前过大结果未被写入任务。", "Split the file and retry explicitly. The oversized result was not written to the task."),
  provider_model_not_found: (locale, source) => providerCopy(locale, source, "模型名称不存在或无权限", "The model was not found or is unavailable", "核对模型名称，或改选另一个已启用模型后重试。", "Check the model name or retry with another enabled model."),
  provider_credentials_unavailable: (locale, source) => providerCopy(locale, source, "无法读取已保存的模型凭据", "Saved model credentials could not be loaded", "重新保存 BYOK 凭据后重试。", "Save the BYOK credentials again, then retry."),
  provider_model_or_endpoint_not_found: (locale, source) => providerCopy(locale, source, "模型名称或接口路径不存在", "The model or endpoint was not found", "在 BYOK 核对模型名称与 Base URL；中转站只填写服务根路径，不要填写完整操作终点。", "Check the model name and base URL in BYOK. Enter the relay service root, not a full operation endpoint."),
  provider_request_rejected: (locale, source) => providerCopy(locale, source, "模型接口未接受本次请求", "The model API did not accept this request", "服务商未接受请求参数或格式，具体参数尚不明确。这不能证明模型不支持图片。核对模型名称、接口地址和 API 协议，或换模型重试。", "The provider rejected the request parameters or format. This does not establish a lack of image support. Check the model name, endpoint and API protocol, or try another model."),
  provider_recitation_blocked: (locale, source) => providerCopy(locale, source, "模型停止了原文转写", "The model stopped transcribing this content", "服务商判定输出可能复述受版权保护的原文（RECITATION），没有返回识别结果。这不代表模型缺少图片能力。请更换识别模型后重试，原文件仍保留。", "The provider blocked possible recitation of copyrighted text (RECITATION). This does not mean the model lacks image support. Change the recognition model and retry; the original file is preserved."),
  provider_content_blocked: (locale, source) => providerCopy(locale, source, "模型限制了这次内容输出", "The model blocked this content output", "服务商因内容策略停止输出。这不代表图片测试失败。请更换模型后重试，原文件仍保留。", "The provider stopped output under its content policy. This is not an image-test failure. Change models and retry; the original file is preserved."),
  provider_upstream_unavailable: (locale, source) => providerCopy(locale, source, "模型服务暂时不可用", "The model service is temporarily unavailable", "稍后重试，或换用另一个已启用模型。", "Retry later or choose another enabled model."),
  provider_response_invalid: (locale, source) => providerCopy(locale, source, "模型返回格式无法解析", "The model returned an invalid structure", "重试本次识别，或更换模型；原文件已保留。", "Retry recognition or change models; the original file is preserved."),
  provider_image_payload_invalid: (locale, source) => providerCopy(locale, source, "图片内容无法组成安全模型请求", "The image could not be encoded for the model request", "重新上传清晰的 JPG、PNG 或 WebP；若持续出现，请携带任务编号联系管理员。", "Upload a clear JPG, PNG, or WebP again. If it persists, share the job ID with an administrator."),
  provider_message_payload_not_supported: (locale, source) => providerCopy(locale, source, "当前协议无法发送这类输入", "The selected protocol cannot encode this input", "核对高级 API 协议，或改用支持当前文件类型的模型。", "Check the Advanced API protocol or choose a model that supports this file type."),
  provider_endpoint_dns_failed: (locale, source) => providerCopy(locale, source, "中转站域名无法解析", "The relay hostname could not be resolved", "检查 Base URL 拼写和中转站服务状态后重试。", "Check the base URL spelling and relay status, then retry."),
  provider_endpoint_non_public_address: (locale, source) => providerCopy(locale, source, "中转站域名解析到非公网地址", "The relay hostname resolved to a non-public address", "检查中转站 DNS 配置后重试；系统已阻止访问内网或特殊地址。", "Check the relay DNS configuration, then retry. Access to private or special addresses was blocked."),
  provider_endpoint_tls_failed: (locale, source) => providerCopy(locale, source, "中转站 HTTPS 证书校验失败", "The relay HTTPS certificate could not be verified", "请中转站维护方修复可信证书；系统不会绕过证书校验。", "Ask the relay operator to fix its trusted certificate. Certificate checks are not bypassed."),
  provider_endpoint_redirect_blocked: (locale, source) => providerCopy(locale, source, "中转站返回了重定向", "The relay returned a redirect", "填写重定向后的实际 HTTPS 服务根路径；系统不会自动跟随重定向。", "Enter the final HTTPS service root. Redirects are not followed automatically."),
  provider_endpoint_protocol_mismatch: (locale, source) => providerCopy(locale, source, "中转站响应与所选 API 协议不一致", "The relay response does not match the selected API protocol", "在 BYOK 的高级设置中核对 OpenAI、Anthropic 或 Gemini 协议。", "Check the OpenAI, Anthropic, or Gemini protocol in BYOK Advanced settings."),
  provider_endpoint_response_too_large: (locale, source) => providerCopy(locale, source, "中转站响应超过安全大小上限", "The relay response exceeded the safety limit", "缩小输入或输出范围后重试；若持续出现，请联系管理员评估限制。", "Reduce the input or requested output and retry. If it persists, ask an administrator to review the limit."),
  submission_parse_invalid: (locale) => ({
    title: tx(locale, "模型返回格式无法解析", "The model returned an invalid structure"),
    description: tx(
      locale,
      "模型有返回内容，但没有形成系统需要的学生、题号和作答结构；系统没有把不确定内容当成成功结果。",
      "The model returned content, but not the required student, question-ID, and answer structure. The system did not treat uncertain output as a success.",
    ),
    nextStep: tx(locale, "可直接重试；若持续出现，换用结构化输出更稳定的模型。", "Retry once; if it persists, choose a model with more reliable structured output."),
  }),
  submission_parse_failed: (locale) => ({
    title: tx(locale, "作答识别没有完成", "Submission recognition did not complete"),
    description: tx(
      locale,
      "系统没有得到可用的结构化作答，也没有收到更具体的安全原因码。原文件与任务编号已保留，可据此继续排查。",
      "No usable structured submission was produced, and no more specific safe code was available. The original file and job ID are preserved for diagnosis.",
    ),
    nextStep: tx(locale, "先重试一次；若仍失败，请携带下方任务编号排查服务端日志。", "Retry once; if it fails again, use the job ID below to inspect server logs."),
  }),
  submission_model_field_too_long: (locale) => ({
    title: tx(locale, "模型返回字段超过安全长度", "The model returned an oversized field"),
    description: tx(
      locale,
      "模型返回了过长的学号、姓名、题号或标签。系统只拒绝这份来源，没有让它拖垮整批，也没有截断后冒充正确结果。",
      "The model returned an overlong student ID, name, question ID, or flag. Only this source was rejected; the batch continued and no truncated value was treated as correct.",
    ),
    nextStep: tx(locale, "重试一次；若持续出现，换用结构化输出更稳定的模型并核对原文件版式。", "Retry once; if it persists, use a model with more reliable structured output and inspect the source layout."),
  }),
  submission_source_persistence_failed: (locale) => ({
    title: tx(locale, "原文件保存未完成", "The original file was not saved"),
    description: tx(
      locale,
      "系统无法确认这份原文件已经安全保存，因此没有继续把它当作可识别来源。这是存储或数据库问题，不是学生答案问题。",
      "The system could not confirm that this original was safely stored, so it did not continue treating it as a recognizable source. This is a storage or database issue, not a student-answer issue.",
    ),
    nextStep: tx(locale, "先重试上传；若重复出现，请携带任务编号让管理员检查文件存储和数据库。", "Retry the upload. If it repeats, share the job ID with an administrator to check file storage and the database."),
  }),
  submission_persistence_failed: (locale) => ({
    title: tx(locale, "识别结果写入任务失败", "Recognized results could not be saved to the task"),
    description: tx(
      locale,
      "原文件和逐文件识别结果已经保留，但系统未能把可用作答发布到当前任务。这是结果持久化问题，不应重新解释为 OCR 或学生答案错误。",
      "The originals and per-file recognition outcomes were preserved, but usable answers could not be published to this task. This is result persistence failure, not an OCR or student-answer error.",
    ),
    nextStep: tx(locale, "请携带任务编号让管理员检查数据库冲突或存储状态，然后重试。", "Share the job ID with an administrator to check database conflicts or storage state, then retry."),
  }),
  submission_outcome_persistence_failed: (locale) => ({
    title: tx(locale, "逐文件识别结果保存未完成", "Per-file outcomes were not fully saved"),
    description: tx(
      locale,
      "原文件已经保存，但系统无法确认每份文件的终态都写入数据库。任务已停止，不会把缺失结果继续显示为处理中，也不会静默进入批改。",
      "The originals were saved, but the system could not confirm that every per-file terminal outcome reached the database. The task stopped; missing results will not remain processing or silently enter grading.",
    ),
    nextStep: tx(locale, "携带任务编号让管理员检查数据库后重试；无需重新整理原始文件。", "Share the job ID with an administrator to check the database, then retry; the originals do not need to be rebuilt."),
  }),
  no_answer_content_detected: (locale) => ({
    title: tx(locale, "没有提取到任何作答内容", "No answer content was detected"),
    description: tx(
      locale,
      "文件文字和模型返回结构都可以读取，但结果中没有任何学生作答。这与“题号不匹配”不同，也不表示学生答错；常见原因是只上传了封面/空白页、答案区域未拍全，或页面版式让模型漏掉了作答。",
      "The file text and model response structure were readable, but no student answers were present in the result. This differs from unmatched question IDs and does not mean the student answered incorrectly. Common causes include a cover/blank page, a cropped answer area, or a layout the model missed.",
    ),
    nextStep: tx(locale, "打开原文件确认答案区域存在且完整；必要时重新拍摄，或换一个识别模型重试。", "Open the original and confirm the answer area is present and complete; rescan or retry with another recognition model if needed."),
  }),
  no_matching_answer: (locale, source) => ({
    title: tx(locale, "没有作答能对应当前任务题目", "No answers match this task's questions"),
    description: tx(
      locale,
      "文件内容已被解析，也提取到了作答条目，但其中的题号没有一个能对应当前任务。它表示“题目不匹配”，不是“学生答错了”。可能是传错作业、题号被 OCR 误读，或任务题目后来被替换。",
      "The file was parsed and answer entries were extracted, but none of their question IDs match this task. This means the questions do not match—not that the student answered incorrectly. The wrong assignment may have been uploaded, OCR may have misread labels, or the task questions may have changed.",
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
  media_inspection_unavailable: (locale) => fileCopy(locale, "文件安全检查暂时不可用", "Media safety inspection is unavailable", "请管理员恢复媒体检查组件；系统尚未把文件发送给百度。", "Ask an administrator to restore media inspection. The file was not sent to Baidu."),
  media_inspection_busy: (locale) => fileCopy(locale, "文件安全检查正忙", "Media safety inspection is busy", "稍等片刻后重试；文件尚未发送给百度。", "Wait briefly and retry. The file has not been sent to Baidu."),
  media_inspection_timeout: (locale) => fileCopy(locale, "文件安全检查超时", "Media safety inspection timed out", "优化或拆分文件后重试；文件尚未发送给百度。", "Optimize or split the file, then retry. The file was not sent to Baidu."),
  media_inspection_failed: (locale) => fileCopy(locale, "文件未通过安全检查", "Media safety inspection failed", "检查文件是否损坏或加密，并重新导出。", "Check whether the file is damaged or encrypted, then export it again."),
  pdf_processing_unavailable: (locale) => fileCopy(locale, "服务器暂时不能处理 PDF", "PDF processing is unavailable on the server", "请管理员恢复 PDF 处理组件；也可临时转为图片或 TXT。", "Ask an administrator to restore PDF processing, or convert the file to an image/TXT temporarily."),
  pdf_extraction_busy: (locale) => fileCopy(locale, "PDF 读取服务正忙", "PDF extraction is busy", "稍等片刻后直接重试；原文件已经保存。", "Wait briefly and retry; the original file is already saved."),
  pdf_extraction_timeout: (locale) => fileCopy(locale, "PDF 读取超时", "PDF extraction timed out", "拆分或优化 PDF 后重试。", "Split or optimize the PDF, then retry."),
  pdf_page_limit_exceeded: (locale) => fileCopy(locale, "PDF 页数超过上限", "The PDF exceeds the page limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_character_limit_exceeded: (locale) => fileCopy(locale, "PDF 文字量超过上限", "The PDF exceeds the text limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_extraction_failed: (locale) => fileCopy(locale, "PDF 内容无法读取", "The PDF could not be read", "检查文件是否损坏或加密，并尝试重新导出。", "Check whether the file is damaged or encrypted, and export it again."),
  pdf_ocr_render_failed: (locale) => fileCopy(locale, "扫描 PDF 无法转成 OCR 页面", "The scanned PDF could not be rendered for OCR", "尝试重新导出 PDF，或逐页转为清晰图片。", "Export the PDF again, or convert each page to a clear image."),
  submission_archive_empty: (locale) => archiveCopy(locale, "压缩包中没有学生文件", "The archive contains no student files", "确认压缩包不是空的，且文件没有全部放在系统隐藏目录中。", "Make sure the archive is not empty and files are not all in hidden system folders."),
  submission_archive_invalid: (locale) => archiveCopy(locale, "压缩包损坏或结构不安全", "The archive is damaged or unsafe", "重新打包为 ZIP，并避免加密、路径穿越或损坏条目。", "Create a new ZIP without encryption, unsafe paths, or damaged members."),
  submission_archive_limit_exceeded: (locale) => archiveCopy(locale, "压缩包超过安全上限", "The archive exceeds a safety limit", "减少文件数量、单文件大小或解压后总体积，再重新上传。", "Reduce file count, member size, or total expanded size, then upload again."),
  submission_archive_member_too_large: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件过大", "This archive member is too large", "只压缩或拆分这份文件后重新打包；同包其他健康文件已经继续处理。", "Compress or split only this member, then rebuild the archive. Other healthy members continued processing."),
  submission_archive_member_unreadable: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件无法读取", "This archive member could not be read", "重新导出这份文件并重新打包；系统保留了整个原始压缩包供核对。", "Export this member again and rebuild the archive. The original container was preserved for review."),
  submission_archive_member_unsafe_path: (locale) => archiveMemberCopy(locale, "压缩包成员路径不安全", "This archive member has an unsafe path", "重新打包时移除绝对路径和 ../ 上级目录；同包其他安全文件已经继续处理。", "Rebuild the archive without absolute paths or ../ parent traversal. Other safe members continued processing."),
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
    description: tx(locale, "系统没有把压缩包中的文件当成成功结果，批次原文件仍然保留。", "No archive members were treated as successful results; the uploaded batch file is preserved."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function archiveMemberCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "这一个成员没有被当成成功结果；整个原始压缩包已保存，同包其他成员不会因此丢失。", "This member was not treated as successful. The original container is saved, and other members are not discarded because of it."),
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
  return { title: tx(locale, "达到本次识别资源上限", "Recognition resource limit reached"), description: tx(locale, "已保留可用内容和缺口状态，没有截断后冒充完整结果。", "Available content and gaps were preserved; truncated content is not marked complete."), nextStep: tx(locale, "缩小题目或页码范围，检查缺失部分；不要直接重复提交整批文件。", "Narrow the question or page range and inspect missing content. Do not blindly resubmit the entire batch.") };
}

function recognitionProcessingCopy(locale: Locale): SubmissionSourceReasonCopy {
  return fileCopy(locale, "本地文件处理未完成", "Local source processing incomplete", "稍后重试本地读取；若反复失败，请核对原件或携带任务编号联系管理员。", "Retry local reading later. If it repeats, inspect the original or contact an administrator with the job ID.");
}

function recognitionOptInCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "此资料尚未启用图像识别", "Image recognition is not enabled for this material"), description: tx(locale, "当前资料需要视觉读取，尚未提交模型调用。", "This material needs visual reading; no model request was submitted."), nextStep: tx(locale, "在资料上传设置中明确启用图像识别，或改用可复制文字版本。", "Explicitly enable image recognition in the upload settings or use a text-based version.") };
}

function recognitionProviderCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "需要匹配的识别或解析模型", "A compatible recognition or parsing model is required"), description: tx(locale, "当前配置不能执行这个阶段；系统没有自动改用其他模型。", "The current configuration cannot run this stage. No other model was selected automatically."), nextStep: tx(locale, "核对本阶段已选模型及凭据；专用 OCR 的结果需要另行选择结构化解析模型。", "Check the selected stage model and credentials. Dedicated OCR output needs a separately selected structured parser.") };
}

function recognitionReviewCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "识别结果需要核对", "Recognition needs review"), description: tx(locale, "内容仍有疑点或结构不完整，不表示学生答错。", "The content is uncertain or incomplete; this is not a student mistake."), nextStep: tx(locale, "对照原文件，在现有复核页面修正或确认可辨认内容。", "Compare with the original and correct or confirm readable content in the review page.") };
}

function recognitionStateCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "识别状态需要核实", "Verify recognition state"), description: tx(locale, "任务仍在处理，或来源、模型及执行状态已变化。系统没有自动重提不确定调用。", "The job is still running, or its source, model or execution state changed. Uncertain calls were not resubmitted automatically."), nextStep: tx(locale, "先刷新任务状态；如持续停滞，携带任务编号核对已提交调用后再决定是否重试。", "Refresh the job first. If it remains stuck, verify submitted calls using the job ID before retrying.") };
}

function recognitionStorageCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "识别证据暂不可用", "Recognition evidence unavailable"), description: tx(locale, "原件或缓存证据的可用性与版本校验未通过，没有使用不匹配内容。", "Source or cached-evidence availability/version validation failed; mismatched content was not used."), nextStep: tx(locale, "刷新后核对原件状态；携带任务编号排查存储，不要反复调用模型。", "Refresh and check the original's status. Diagnose storage using the job ID instead of repeating model calls.") };
}

function recognitionScopeCopy(locale: Locale): SubmissionSourceReasonCopy {
  return { title: tx(locale, "请缩小或明确识别范围", "Narrow or clarify the recognition scope"), description: tx(locale, "目标题未能可靠定位，或所选范围超过本次上限。", "Target questions were not reliably located or the selected scope exceeded this request's limit."), nextStep: tx(locale, "补充题号及页码，再识别需要的范围；未找到不等于原文不存在。", "Provide question IDs and page hints, then recognize the needed scope. Not located does not mean absent.") };
}

import { getAPIErrorCode, getAPIErrorDetail, normalizeAPIError } from "@/api/client";
import type { Locale } from "@/i18n/messages";
import type { ExpertConfig, ResultArtifactStatus, Task, TaskLite, TaskStatus } from "@/types";

const WORKFLOW_REVISION_CONFLICT_CODES = new Set([
  "stale_revision",
  "task_workflow_changed",
  "version_conflict",
]);

export function isWorkflowRevisionConflictCode(code: string | null | undefined): boolean {
  return Boolean(code && WORKFLOW_REVISION_CONFLICT_CODES.has(code));
}

export function isCurrentResultArtifactReady({
  finalResultDirty,
  status,
  fileCount,
}: {
  finalResultDirty: boolean;
  status: ResultArtifactStatus | undefined;
  fileCount: number;
}): boolean {
  return !finalResultDirty && status === "ready" && fileCount > 0;
}

export interface ModelReadiness {
  isLoading: boolean;
  isError: boolean;
  enabledCount: number;
  hasEnabledExpert: boolean;
  disabledReason: string | null;
}

export function getModelReadiness({
  experts,
  isLoading,
  isError,
}: {
  experts: ExpertConfig[] | undefined;
  isLoading: boolean;
  isError: boolean;
}): ModelReadiness {
  const enabledCount = (experts ?? []).filter((expert) => expert.enabled).length;
  const loaded = !isLoading && !isError;

  return {
    isLoading,
    isError,
    enabledCount,
    hasEnabledExpert: enabledCount > 0,
    disabledReason: loaded && enabledCount === 0 ? "需要先启用至少一个 BYOK 专家。" : null,
  };
}

export type UploadKind = "problems" | "submissions";

const RESULT_STATUSES = new Set<TaskStatus>([
  "graded", "review_confirmed", "generating_analysis", "finalized",
]);

export interface UploadGuard {
  disabled: boolean;
  reason: string | null;
  confirmTitle: string | null;
  confirmMessage: string | null;
  suggestNewTask: boolean;
}

export function getUploadGuard({
  kind,
  task,
  isUploading,
  isProcessing,
  modelReadiness,
}: {
  kind: UploadKind;
  task?: Task | TaskLite;
  isUploading: boolean;
  isProcessing: boolean;
  modelReadiness: ModelReadiness;
}): UploadGuard {
  if (isUploading) {
    return disabled("正在上传文件，请稍候。");
  }
  if (isProcessing) {
    return disabled("当前任务正在后台处理中，请等当前阶段完成后再重新上传。");
  }
  if (modelReadiness.isError) {
    return disabled("暂时无法确认 BYOK 专家状态，请刷新后再试。");
  }
  if (modelReadiness.disabledReason) {
    return disabled(modelReadiness.disabledReason);
  }

  if (!task) {
    return {
      disabled: false,
      reason: null,
      confirmTitle: null,
      confirmMessage: null,
      suggestNewTask: false,
    };
  }

  if (kind === "problems" && (task.problem_file_name || task.problem_count > 0)) {
    const hasDownstreamData = task.student_count > 0 || RESULT_STATUSES.has(task.status) || task.status === "grading";
    return {
      disabled: false,
      reason: null,
      confirmTitle: "确认替换题目文件？",
      confirmMessage: hasDownstreamData
        ? "替换题目会覆盖已识别的题目，并可能让已有学生作答、批改结果和分析不再匹配。若只是想批改另一份作业，建议新建任务。"
        : "替换题目会覆盖当前已识别的题目内容。确认继续上传新文件吗？",
      suggestNewTask: hasDownstreamData,
    };
  }

  if (kind === "submissions" && (task.submission_file_name || task.student_count > 0)) {
    return {
      disabled: task.status === "grading" || task.status === "generating_analysis",
      reason: task.status === "grading"
        ? "批改正在进行中，不能替换学生作答。"
        : task.status === "generating_analysis"
          ? "分析正在生成中，不能替换学生作答。"
          : null,
      confirmTitle: "确认替换学生作答？",
      confirmMessage:
        RESULT_STATUSES.has(task.status)
          ? "替换学生作答会覆盖已识别的作答，并使已有批改结果需要重新生成。若只是想批改另一批学生，建议新建任务。"
          : "替换学生作答会覆盖当前已识别的作答内容。确认继续上传新文件吗？",
      suggestNewTask: RESULT_STATUSES.has(task.status),
    };
  }

  return {
    disabled: false,
    reason: null,
    confirmTitle: null,
    confirmMessage: null,
    suggestNewTask: false,
  };
}

export interface GradingGuard {
  disabled: boolean;
  reason: string | null;
}

export function getGradingGuard({
  status,
  problemCount,
  studentCount,
  isPending,
  modelReadiness,
}: {
  status: TaskStatus;
  problemCount: number;
  studentCount: number;
  isPending: boolean;
  modelReadiness: ModelReadiness;
}): GradingGuard {
  if (isPending) {
    return { disabled: true, reason: "正在启动批改。" };
  }
  if (modelReadiness.isError) {
    return { disabled: true, reason: "暂时无法确认 BYOK 专家状态，请刷新后再试。" };
  }
  if (modelReadiness.disabledReason) {
    return { disabled: true, reason: modelReadiness.disabledReason };
  }
  if (problemCount <= 0) {
    return { disabled: true, reason: "请先上传并校对题目。" };
  }
  if (studentCount <= 0) {
    return { disabled: true, reason: "请先上传并校对学生作答。" };
  }
  if (status !== "submissions_ready") {
    if (status === "grading") {
      return { disabled: true, reason: "批改已经在进行中。" };
    }
    if (RESULT_STATUSES.has(status)) {
      return { disabled: true, reason: "批改已完成，请进入结果复核。" };
    }
    return { disabled: true, reason: "请先完成题目和作答校对，再开始批改。" };
  }
  return { disabled: false, reason: null };
}

export interface RecoverableErrorInfo {
  title: string;
  description: string;
  actionLabel: string;
  actionHref?: string;
  actionKind: RecoverableActionKind;
  tone: "danger" | "warning" | "primary";
  retryAfterSeconds?: number;
  technicalDetails: Array<{ label: string; value: string }>;
}

export type RecoverableActionKind =
  | "retry"
  | "byok"
  | "reupload"
  | "reselect"
  | "refresh"
  | "adjust_experts";

export interface RecoverableErrorContext {
  locale?: Locale;
  phase?: string;
  jobId?: string | null;
  returnTo?: string;
}

const BYOK_CODES = new Set([
  "no_provider_configured",
  "provider_credentials_unavailable",
  "recognition_provider_not_enabled",
  "provider_not_enabled",
  "provider_auth_failed",
  "vision_provider_required",
  "shared_pool_kb_requires_byok",
  "no_enabled_expert",
  "expert_verification_auth_failed",
  // "vision_provider_required" has its own dedicated branch below (OCR-specific copy).
]);

const FILE_CODES = new Set([
  "ocr_empty_result",
  "pdf_extraction_failed",
  "pdf_ocr_render_failed",
  "pdf_processing_unavailable",
  "source_decode_failed",
  "source_empty",
  "source_text_too_large",
  "source_too_large",
  "source_type_not_allowed",
  "source_mime_type_not_allowed",
  "problem_source_unsupported",
  "problem_source_decode_failed",
  "problem_source_character_limit_exceeded",
  "problem_source_token_limit_exceeded",
  "pdf_page_limit_exceeded",
  "pdf_character_limit_exceeded",
  "submission_source_unsupported",
  "submission_source_content_type_mismatch",
  "submission_source_empty",
  "submission_source_too_large",
  "submission_archive_empty",
  "submission_archive_invalid",
  "submission_archive_limit_exceeded",
  "submission_archive_member_too_large",
  "submission_archive_member_unreadable",
  "submission_archive_member_unsafe_path",
  "submission_roster_unsupported",
  "submission_roster_empty",
  "submission_roster_too_large",
  "submission_roster_too_many_rows",
  "submission_roster_headers_invalid",
]);

const GRADING_CONFIGURATION_CODES = new Set([
  "grading_provider_configuration_changed",
  "grading_provider_selection_invalid",
  "grading_setup_invalid",
]);

const GRADING_INPUT_CODES = new Set([
  "grading_inputs_changed",
  "grading_question_snapshot_invalid",
  "grading_question_snapshot_missing",
]);

const SOURCE_CHANGED_CODES = new Set([
  "question_preparation_source_expired",
  "question_preparation_library_source_changed",
  "problem_source_material_changed",
  "material_import_source_changed",
  "material_import_source_unavailable",
  "material_import_plan_expired",
]);

export function classifyRecoverableError(
  error: unknown,
  context: RecoverableErrorContext = {},
): RecoverableErrorInfo {
  const apiError = normalizeAPIError(error);
  const locale = context.locale ?? "zh-CN";
  const code = getAPIErrorCode(apiError) ?? stableBackgroundErrorCode(error);
  const detail = getAPIErrorDetail(apiError);
  const message = apiError.message || "请求失败，请稍后重试。";
  const normalized = `${code ?? ""} ${message}`.toLowerCase();
  const technicalDetails = buildTechnicalDetails(apiError.status, code, detail, context, locale);
  const retryAfterSeconds = apiError.retryAfterSeconds;
  const returnTo = context.returnTo?.trim();

  if (code && GRADING_CONFIGURATION_CODES.has(code)) {
    return {
      title: tx(locale, "批改模型配置已经变化", "The grading model configuration changed"),
      description: tx(
        locale,
        "本次批改使用的模型或批改设置在任务确认后发生了变化。请重新打开批改设置，确认当前可用模型后再启动批改。",
        "The model or grading setup changed after this run was confirmed. Reopen grading settings, confirm the currently available model, and start grading again.",
      ),
      actionLabel: tx(locale, "调整批改设置", "Review grading settings"),
      actionKind: "adjust_experts",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code && GRADING_INPUT_CODES.has(code)) {
    return {
      title: tx(locale, "批改输入已经变化或不完整", "The grading inputs changed or are incomplete"),
      description: tx(
        locale,
        "题目、作答或本次批改快照已不再匹配。任务原资料仍然保留；请刷新后重新启动批改，以当前内容生成新的批次。",
        "The questions, submissions, or grading snapshot no longer match. Source data is preserved. Refresh and start a new run from the current content.",
      ),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "grading_persistence_failed") {
    return {
      title: tx(locale, "批改结果保存失败", "The grading results could not be saved"),
      description: tx(
        locale,
        "批改服务未能把本批结果完整写入数据库，因此没有把不完整结果标为成功。任务原资料仍然保留，请稍后重试；若持续失败，请把任务编号交给管理员检查存储服务。",
        "The service could not fully save this grading batch, so incomplete results were not marked successful. Source data is preserved. Retry later; if it persists, give the job ID to an administrator to check storage.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "grading_failed") {
    return {
      title: tx(locale, "本次批改没有完成", "This grading run did not finish"),
      description: tx(
        locale,
        "批改过程中出现了未预期的错误，任务资料仍然保留。请稍后重试；若多次重试仍失败，请记下下方任务/作业编号并联系管理员。",
        "An unexpected error occurred during grading. Task data is preserved. Retry shortly; if it keeps failing, note the job ID below and contact your administrator.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "provider_timeout") {
    return {
      title: tx(locale, "模型响应超时", "The model took too long to respond"),
      description: tx(
        locale,
        "模型服务未能在限定时间内返回结果，通常因为负载较高或网络较慢。任务资料不会丢失，请稍后重试。",
        "The model service did not respond in time, usually because it is busy or the connection is slow. Your task data is preserved — retry shortly.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_unreachable") {
    return {
      title: tx(locale, "无法连接模型服务", "Cannot reach the model service"),
      description: tx(
        locale,
        "SmarTAI 连接不到模型服务。请检查网络是否正常、代理或 VPN（科学上网）是否已开启后重试；本地部署请确认后端地址可达。",
        "SmarTAI could not reach the model service. Check your network and that your proxy or VPN is enabled, then retry. For local setups, confirm the backend address is reachable.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_auth_failed") {
    return {
      title: tx(locale, "模型密钥或授权无效", "Model API key or authorization is invalid"),
      description: tx(
        locale,
        "模型返回了授权错误，可能是密钥无效、额度未开通，或当前账号无权使用该模型（例如所选模型不在套餐内）。请在“模型与 BYOK”更新密钥并确认模型可用后再重试。",
        "The model returned an authorization error — the key may be invalid, quota not enabled, or your account lacks access to this model (for example, it isn't included in your plan). Update the key in Models & BYOK and confirm access, then retry.",
      ),
      actionLabel: tx(locale, "前往 BYOK 配置", "Open BYOK settings"),
      actionHref: `/settings/byok${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`,
      actionKind: "byok",
      tone: "primary",
      technicalDetails,
    };
  }

  if (code === "provider_vision_not_supported") {
    return {
      title: tx(locale, "所选模型不支持视觉输入", "The selected model does not support visual input"),
      description: tx(
        locale,
        "这份图片或扫描版 PDF 需要视觉输入，但本阶段明确选择的模型拒绝了图片。原文件和已完成步骤均已保留；请在当前阶段改选支持视觉的模型后重试。",
        "This image or scanned PDF requires visual input, but the model explicitly selected for this stage rejected images. The original and completed steps are preserved; choose a vision-capable model for this stage and retry.",
      ),
      actionLabel: tx(locale, "改选模型后重试", "Switch model and retry"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_model_not_found") {
    return {
      title: tx(locale, "模型名称不可用", "The model name is unavailable"),
      description: tx(locale, "服务商找不到当前模型，或这个 API Key 无权使用它。请核对模型名称或在当前阶段改选其他模型；原文件已保留。", "The provider could not find this model, or the API key cannot access it. Check the model name or choose another model for this stage; the original is preserved."),
      actionLabel: tx(locale, "改选模型后重试", "Switch model and retry"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_request_rejected") {
    return {
      title: tx(locale, "服务商拒绝了请求", "The provider rejected the request"),
      description: tx(locale, "服务商没有接受当前模型或接口参数。请检查模型名称与中转地址，或改选其他模型后重试；原文件已保留。", "The provider did not accept the current model or endpoint parameters. Check the model name and relay URL, or choose another model and retry; the original is preserved."),
      actionLabel: tx(locale, "检查配置后重试", "Check configuration and retry"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "vision_provider_required") {
    const byokReturnTo = context.returnTo?.trim();
    return {
      title: tx(locale, "尚未选择可用的视觉模型", "No usable vision model is selected"),
      description: tx(
        locale,
        "这份文件需要图像识别（OCR），但当前阶段没有可用的视觉模型。请添加或启用支持视觉输入的模型后重试，或上传可复制文字版文件。",
        "This file needs image recognition (OCR), but no usable vision model is available for this stage. Add or enable a vision-capable model and retry, or upload a text-based file.",
      ),
      actionLabel: tx(locale, "选择支持 OCR 的模型", "Choose an OCR-capable model"),
      actionHref: `/settings/byok${byokReturnTo ? `?returnTo=${encodeURIComponent(byokReturnTo)}` : ""}`,
      actionKind: "byok",
      tone: "primary",
      technicalDetails,
    };
  }

  if (code === "ocr_empty_result") {
    return {
      title: tx(locale, "OCR 没有读到可用文字", "OCR found no usable text"),
      description: tx(
        locale,
        "模型完成了图片读取，但返回内容为空。请检查照片清晰度、方向、反光和裁切；也可以换一个视觉模型重试。",
        "The image request completed, but OCR returned no text. Check clarity, orientation, glare, and cropping, or retry with another vision model.",
      ),
      actionLabel: tx(locale, "重新选择文件", "Choose the file again"),
      actionKind: "reupload",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "no_matching_answer") {
    return {
      title: tx(locale, "作答与当前任务题目不匹配", "The answers do not match this task"),
      description: tx(
        locale,
        "文件内容已解析，也提取到了作答条目，但没有题号能对应当前任务。这不是“学生答错”，更可能是传错作业、OCR 误读题号，或任务题目后来被替换。",
        "The file was parsed and answer entries were extracted, but none of their question IDs match this task. This does not mean the student answered incorrectly; the wrong assignment may have been uploaded, OCR may have misread labels, or the task questions may have changed.",
      ),
      actionLabel: tx(locale, "核对并重新上传", "Review and upload again"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "no_answer_content_detected") {
    return {
      title: tx(locale, "没有提取到任何作答内容", "No answer content was detected"),
      description: tx(
        locale,
        "文件文字和模型返回结构都可以读取，但没有提取到学生作答。这不是学生答错，也不同于题号不匹配；请检查是否只上传了封面/空白页、答案区域是否被裁掉，或换一个识别模型重试。",
        "The file text and model response structure were readable, but no student answers were extracted. This is not a wrong answer and differs from unmatched question IDs. Check for a cover/blank page or cropped answer area, or retry with another model.",
      ),
      actionLabel: tx(locale, "核对并重新上传", "Review and upload again"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "submission_source_persistence_failed") {
    return {
      title: tx(locale, "原文件保存未完成", "The original file was not saved"),
      description: tx(
        locale,
        "系统无法确认原文件已经安全保存，因此没有继续识别。这是文件存储或数据库问题，不是学生答案问题。请重试；若重复出现，请把任务编号交给管理员。",
        "The system could not confirm that the original was safely stored, so recognition did not continue. This is a file-storage or database issue, not a student-answer issue. Retry; if it repeats, share the job ID with an administrator.",
      ),
      actionLabel: tx(locale, "重新上传", "Upload again"),
      actionKind: "reupload",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_persistence_failed") {
    return {
      title: tx(locale, "识别结果写入任务失败", "Recognized results could not be saved to the task"),
      description: tx(
        locale,
        "原文件和逐文件识别结果已经保留，但可用作答没有成功发布到当前任务。这是结果持久化问题，不是 OCR 或学生答案错误；请携带任务编号排查数据库后重试。",
        "The originals and per-file outcomes were preserved, but usable answers were not published to this task. This is result persistence failure, not an OCR or student-answer error. Use the job ID to check the database, then retry.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_outcome_persistence_failed") {
    return {
      title: tx(locale, "逐文件识别结果保存未完成", "Per-file outcomes were not fully saved"),
      description: tx(
        locale,
        "原文件已经保存，但系统无法确认每份来源的终态都已写入数据库。任务已停止，不会把缺失结果静默带入批改。请携带任务编号排查数据库后重试。",
        "The originals were saved, but the system could not confirm every per-source terminal outcome in the database. The task stopped and will not silently grade missing results. Use the job ID to check the database, then retry.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_parse_invalid") {
    return {
      title: tx(locale, "模型返回格式无法解析", "The model returned an invalid structure"),
      description: tx(
        locale,
        "模型有返回内容，但没有形成系统需要的学生、题号和作答结构；系统没有把不确定内容当成成功结果。可以重试，或换用结构化输出更稳定的模型。",
        "The model returned content, but not the required student, question-ID, and answer structure. The system did not accept uncertain output as a success. Retry or choose a model with more reliable structured output.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_model_field_too_long") {
    return {
      title: tx(locale, "模型返回字段超过安全长度", "The model returned an oversized field"),
      description: tx(
        locale,
        "模型返回了过长的身份或题号字段。系统只拒绝这份来源，没有截断后冒充成功，也不会拖垮整批。",
        "The model returned an overlong identity or question field. Only this source was rejected; no truncated value was accepted and the rest of the batch continued.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "pdf_extraction_busy" || code === "pdf_extraction_timeout") {
    return {
      title: code === "pdf_extraction_busy"
        ? tx(locale, "PDF 读取服务正忙", "PDF extraction is busy")
        : tx(locale, "PDF 读取超时", "PDF extraction timed out"),
      description: code === "pdf_extraction_busy"
        ? tx(locale, "原文件已保存。稍等片刻后直接重试，不需要重新整理整批文件。", "The original is saved. Wait briefly and retry; the batch does not need to be rebuilt.")
        : tx(locale, "原文件已保存，但本次 PDF 读取超过时间上限。请拆分或优化 PDF 后重试。", "The original is saved, but PDF extraction exceeded its time limit. Split or optimize the PDF, then retry."),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (
    (code && BYOK_CODES.has(code))
    || normalized.includes("api key")
    || normalized.includes("byok")
    || normalized.includes("provider_not_enabled")
    || normalized.includes("provider not enabled")
    || normalized.includes("no enabled expert")
  ) {
    return {
      title: tx(locale, "需要配置可用模型", "A model configuration is required"),
      description: tx(
        locale,
        "当前没有可用于此操作的模型。前往 BYOK 添加或启用模型后，可以回到当前页面继续。",
        "No model is available for this action. Add or enable a BYOK model, then return here to continue.",
      ),
      actionLabel: tx(locale, "前往 BYOK 配置", "Open BYOK settings"),
      actionHref: `/settings/byok${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`,
      actionKind: "byok",
      tone: "primary",
      technicalDetails,
    };
  }

  if (
    apiError.status === 429
    || code === "provider_rate_limited"
    || normalized.includes("rate limit")
    || normalized.includes("quota")
    || normalized.includes("too many requests")
  ) {
    const wait = retryAfterSeconds === undefined
      ? ""
      : tx(locale, ` 建议 ${formatWait(retryAfterSeconds, locale)}后重试。`, ` Retry in ${formatWait(retryAfterSeconds, locale)}.`);
    return {
      title: tx(locale, "模型限额暂时不可用", "The model rate limit is temporarily unavailable"),
      description: tx(
        locale,
        `请求已被模型服务限流；当前任务和已上传资料不会丢失。${wait}`,
        `The model service rate-limited this request. The task and uploaded materials are preserved.${wait}`,
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      retryAfterSeconds,
      technicalDetails,
    };
  }

  if (
    (code && SOURCE_CHANGED_CODES.has(code))
    || normalized.includes("source expired")
    || normalized.includes("source changed")
    || normalized.includes("source unavailable")
  ) {
    return {
      title: tx(locale, "资料来源已变化", "The source material has changed"),
      description: tx(
        locale,
        "为避免把旧资料识别结果写入当前任务，请重新选择或上传这份资料，再启动识别。",
        "To avoid writing stale results into this task, select or upload this material again before restarting recognition.",
      ),
      actionLabel: tx(locale, "重新选择资料", "Select material again"),
      actionKind: "reselect",
      tone: "warning",
      technicalDetails,
    };
  }

  if (
    (code && FILE_CODES.has(code))
    || apiError.status === 413
    || normalized.includes("unsupported problem source")
    || normalized.includes("no extractable text")
    || normalized.includes("file is empty")
    || normalized.includes("contains no usable text")
  ) {
    return {
      title: tx(locale, "这份文件暂时无法处理", "This file cannot be processed"),
      description: fileErrorDescription(code, message, detail, locale),
      actionLabel: tx(locale, "重新选择文件", "Choose another file"),
      actionKind: "reupload",
      tone: "danger",
      technicalDetails,
    };
  }

  if (
    apiError.status === 409
    || normalized.includes("stale_revision")
    || normalized.includes("workflow_busy")
    || normalized.includes("already_running")
  ) {
    return {
      title: tx(locale, "任务状态已经变化", "The task state has changed"),
      description: friendlyMessage(message, code, tx(locale, "刷新后会按最新阶段继续，不会重复启动同一任务。", "Refresh to continue from the latest stage without starting duplicate work.")),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (
    apiError.status === 0
    && (normalized.includes("network")
      || normalized.includes("timeout")
      || normalized.includes("timed out")
      || normalized.includes("failed to fetch")
      || normalized.includes("waking up"))
  ) {
    return {
      title: tx(locale, "网络或后端暂时不可用", "The network or backend is temporarily unavailable"),
      description: message.includes("waking up")
        ? tx(locale, "后端可能正在唤醒，当前页面内容不会丢失。稍等片刻后重试。", "The backend may be waking up. This page will keep its content; retry shortly.")
        : friendlyMessage(message, code, tx(locale, "请检查网络后重试；当前页面内容不会丢失。", "Check your network and retry. This page will keep its content.")),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  return {
    title: apiError.status >= 500
      ? tx(locale, "后端处理未完成", "Backend processing did not complete")
      : tx(locale, "本次操作未完成", "This action did not complete"),
    description: friendlyMessage(
      message,
      code,
      tx(
        locale,
        "识别或批改过程中出现了未预期的错误，任务资料仍然保留。请稍后重试；若多次重试仍失败，请记下下方任务/作业编号并联系管理员。",
        "An unexpected error occurred. Your task data is preserved. Retry shortly; if it keeps failing, note the job ID below and contact your administrator.",
      ),
    ),
    actionLabel: tx(locale, "重新尝试", "Try again"),
    actionKind: "retry",
    tone: "danger",
    technicalDetails,
  };
}

function buildTechnicalDetails(
  status: number,
  code: string | null,
  detail: Record<string, unknown> | null,
  context: RecoverableErrorContext,
  locale: Locale,
): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string | number | null | undefined }> = [
    { label: "HTTP", value: status || undefined },
    { label: tx(locale, "错误代码", "Error code"), value: code },
    { label: tx(locale, "处理阶段", "Phase"), value: context.phase },
    { label: tx(locale, "任务编号", "Job ID"), value: context.jobId },
    { label: tx(locale, "重试等待", "Retry after"), value: safeTechnicalValue(detail?.retry_after_seconds ?? detail?.retry_after) },
    { label: tx(locale, "页数上限", "Page limit"), value: safeTechnicalValue(detail?.max_pages) },
    { label: tx(locale, "字符上限", "Character limit"), value: safeTechnicalValue(detail?.max_characters) },
    { label: tx(locale, "文件上限", "File-size limit"), value: formatByteLimit(detail?.max_bytes) },
  ];
  return rows
    .filter((row) => row.value !== null && row.value !== undefined && row.value !== "")
    .map((row) => ({ label: row.label, value: String(row.value) }));
}

function safeTechnicalValue(value: unknown): string | number | undefined {
  return typeof value === "string" || typeof value === "number" ? value : undefined;
}

function stableBackgroundErrorCode(error: unknown): string | null {
  if (typeof error !== "string") return null;
  const value = error.trim();
  return /^[a-z][a-z0-9_]{1,127}$/.test(value) ? value : null;
}

/**
 * Short, localized label for a background error code/message — for inline strips
 * that would otherwise leak a raw code (e.g. "grading_failed") to the user.
 *
 * Bare snake_case codes route through classifyRecoverableError's code branches;
 * genuine event messages (e.g. "OCR returned empty text for x.pdf") are returned
 * verbatim so they stay readable. `null`/empty yields "".
 */
export function backgroundErrorTitle(value: unknown, locale: Locale = "zh-CN"): string {
  if (value == null) return "";
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return "";
    if (!stableBackgroundErrorCode(text)) return text;
  }
  return classifyRecoverableError(value, { locale }).title;
}

function fileErrorDescription(
  code: string | null,
  message: string,
  detail: Record<string, unknown> | null,
  locale: Locale,
): string {
  if (code === "source_too_large") {
    const limit = formatByteLimit(detail?.max_bytes);
    return limit
      ? tx(locale, `文件超过 ${limit} 的单文件上传上限，请选择更小的文件。`, `The file exceeds the ${limit} per-file upload limit. Choose a smaller file.`)
      : tx(locale, "文件超过单文件上传上限，请选择更小的文件。", "The file exceeds the per-file upload limit. Choose a smaller file.");
  }
  if (code === "problem_source_decode_failed" || code === "source_decode_failed") {
    return tx(locale, "没有从文件中读取到可用正文。若是扫描 PDF，请先转换为可复制文字的 PDF、TXT 或 Markdown。", "No usable text could be read. If this is a scanned PDF, convert it to a text-based PDF, TXT, or Markdown file first.");
  }
  if (code === "ocr_empty_result") {
    return tx(locale, "视觉模型没有从图片或扫描页中识别出可用文字。请检查图片清晰度、方向和页面内容，或更换视觉模型后重试。", "The vision model found no usable text in the image or scanned page. Check clarity, orientation, and page content, or retry with another vision model.");
  }
  if (code === "submission_archive_invalid") {
    return tx(locale, "压缩包损坏或包含不安全的文件路径。请重新打包为正常 ZIP 后上传。", "The archive is damaged or contains unsafe paths. Create a clean ZIP archive and upload it again.");
  }
  if (code === "submission_archive_limit_exceeded") {
    return tx(locale, "压缩包中的文件数量、单文件大小或解压后总大小超过安全上限。请拆分压缩包后重新上传。", "The archive exceeds the safe file-count, per-file, or expanded-size limit. Split it into smaller archives and upload again.");
  }
  if (code === "pdf_page_limit_exceeded") {
    return tx(locale, "PDF 页数超出本次处理上限。请拆分文件后重新上传。", "The PDF exceeds the page limit. Split it into smaller files and upload again.");
  }
  if (["problem_source_character_limit_exceeded", "problem_source_token_limit_exceeded", "pdf_character_limit_exceeded"].includes(code ?? "")) {
    return tx(locale, "文件正文过长。请拆分内容，或通过“从原文提取”限定章节与题号。", "The document is too long. Split it, or use Extract from source to limit chapters and question numbers.");
  }
  if (code?.includes("roster_headers")) {
    return tx(locale, "名单必须包含可识别的学号和姓名列。请修正表头后重新上传。", "The roster must contain recognizable student-ID and name columns. Fix the headers and upload it again.");
  }
  return friendlyMessage(message, code, tx(locale, "请检查文件格式与内容后重新选择。", "Check the file format and content, then choose it again."));
}

function formatByteLimit(value: unknown): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return undefined;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
  const megabytes = value / (1024 * 1024);
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`;
}

function friendlyMessage(message: string, code: string | null, fallback: string): string {
  const trimmed = message.trim();
  return trimmed && trimmed !== code && trimmed !== "Unknown API error" ? trimmed : fallback;
}

function formatWait(seconds: number, locale: Locale): string {
  if (seconds < 60) return tx(locale, `${seconds} 秒`, `${seconds} seconds`);
  const minutes = Math.ceil(seconds / 60);
  return tx(locale, `${minutes} 分钟`, `${minutes} minutes`);
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

function disabled(reason: string): UploadGuard {
  return {
    disabled: true,
    reason,
    confirmTitle: null,
    confirmMessage: null,
    suggestNewTask: false,
  };
}

import { isTaskAwaitingGrading } from "@/lib/taskFlow";
import { getAPIErrorCode, getAPIErrorDetail, normalizeAPIError } from "@/api/client";
import type { Locale } from "@/i18n/messages";
import {
  isKnowledgeStorageQuotaExceeded,
  knowledgeStorageQuotaCopy,
} from "@/lib/knowledgeStorage";
import type { ExpertConfig, ProviderFailure, ResultArtifactStatus, Task, TaskLite, TaskStatus } from "@/types";

const WORKFLOW_REVISION_CONFLICT_CODES = new Set([
  "stale_revision",
  "task_workflow_changed",
  "version_conflict",
  "workflow_revision_conflict",
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
  "graded", "finalized",
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
      disabled: task.status === "grading",
      reason: task.status === "grading"
        ? "批改正在进行中，不能替换学生作答。"
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
  if (!isTaskAwaitingGrading(status)) {
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
  dailyQuotaExhausted?: boolean;
  retryUnavailable?: boolean;
  showUsageLinks?: boolean;
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
  taskId?: string;
  returnTo?: string;
  providerFailures?: ProviderFailure[];
}

const BYOK_CODES = new Set([
  "no_provider_configured",
  "provider_credentials_unavailable",
  "recognition_provider_not_enabled",
  "provider_not_enabled",
  "provider_auth_failed",
  "ocr_credential_not_found",
  "provider_permission_denied",
  "provider_region_unsupported",
  "vision_provider_required",
  "shared_pool_kb_requires_byok",
  "no_enabled_expert",
  "expert_verification_auth_failed",
  // "vision_provider_required" has its own dedicated branch below (OCR-specific copy).
]);

const FILE_CODES = new Set([
  "source_content_type_not_allowed",
  "pdf_invalid", "pdf_encrypted", "pdf_page_out_of_range",
  "image_invalid", "image_format_mismatch", "image_mode_unsupported",
  "image_multiframe_unsupported", "image_pixel_limit_exceeded",
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
  "ocr_input_invalid",
  "ocr_unsupported_file",
  "ocr_file_too_large",
  "ocr_image_dimension_limit_exceeded",
  "media_inspection_unavailable",
  "media_inspection_busy",
  "media_inspection_timeout",
  "media_inspection_failed",
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
  "question_preparation_source_unavailable",
  "question_preparation_retry_source_unavailable",
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
  const taskId = context.taskId?.trim();
  const taskHref = (suffix: string) => taskId
    ? `/tasks/${encodeURIComponent(taskId)}${suffix}`
    : undefined;

  if (code === "question_source_pages_out_of_range") {
    return {
      title: tx(locale, "请调整识别范围", "Adjust the recognition scope"),
      description: tx(locale, "填写的页码超出了 PDF 总页数。请使用文件中的页序号，而非书上印刷的页码。这份资料尚未调用识别模型。", "A page is outside this PDF. Use file page positions, not printed page numbers. No recognition model was called for this source."),
      actionLabel: tx(locale, "修改资料范围", "Edit source scope"),
      actionKind: "reselect", actionHref: taskHref("/upload/problems"),
      tone: "warning", technicalDetails,
    };
  }

  if (code === "question_source_incomplete") {
    return {
      title: tx(locale, "资料尚未完整读取", "Source reading is incomplete"),
      description: tx(locale, "部分页面未能读取，题目生成已暂停。请检查资料后重试。", "Some pages could not be read, so question generation is paused. Check the source and try again."),
      actionLabel: tx(locale, "检查资料", "Check source"), actionKind: "reselect",
      actionHref: taskHref("/upload/problems"), tone: "warning", technicalDetails,
    };
  }

  if (code === "problem_extraction_failed") {
    return {
      title: tx(locale, "题目识别未完成", "Question recognition did not complete"),
      description: tx(locale, "请检查原文件和识别描述后重试，也可以人工继续补充题目。", "Check the source file and recognition instructions, then retry, or continue manually to add questions."),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry", tone: "danger", technicalDetails,
    };
  }

  if (code === "recognition_already_running") {
    return {
      title: tx(locale, "这份资料仍在识别", "This source is still being recognized"),
      description: tx(locale, "这份资料正在处理，请查看进度。", "This source is being processed. Check its progress."),
      actionLabel: tx(locale, "查看任务", "View task"), actionHref: taskHref(""),
      actionKind: "refresh", tone: "warning", technicalDetails,
    };
  }

  if (code === "ocr_provider_grading_not_supported") {
    return {
      title: tx(locale, "该 OCR 服务不支持批改", "This OCR service does not support grading"),
      description: tx(
        locale,
        "百度 Unlimited-OCR 用于识别文字。请返回批改设置，选择批改模型。",
        "Baidu Unlimited-OCR recognizes text. Return to Grading Setup and choose a grading model.",
      ),
      actionLabel: tx(locale, "更换批改模型", "Choose another grading model"),
      actionKind: "adjust_experts",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_submit_uncertain") {
    const observed = Array.isArray(context.providerFailures) ? context.providerFailures.at(-1) : undefined;
    const cause = observed ? uncertainProviderCauseCopy(observed, locale) : null;
    if (cause && observed) {
      technicalDetails.push({ label: tx(locale, "最近模型故障", "Latest model failure"), value: observed.code });
      if (cause.httpStatus) {
        technicalDetails.push({ label: tx(locale, "供应商 HTTP 状态", "Provider HTTP status"), value: String(cause.httpStatus) });
      }
    }
    return {
      title: cause?.title ?? tx(locale, "模型调用未完成，重试需确认", "The model call did not complete; confirm before retrying"),
      description: (cause ? `${cause.description} ` : tx(locale,
        "本次未取得可用结果，现有记录无法确定具体原因。",
        "No usable result was received, and the available records do not identify the cause. ",
      )) + tx(
        locale,
        "上次调用是否计费尚无法确认，重试可能再次消耗额度。原文件和已保存结果保留。",
        "The previous call’s billing status is unknown. Retrying may use additional quota. Originals and saved results are retained.",
      ),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "danger",
      technicalDetails,
    };
  }

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
        "任务内容已更新，请刷新后重新开始批改。",
        "Task content changed. Refresh before starting a new grading run.",
      ),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "grading_setup_required") {
    return {
      title: tx(locale, "批改设置尚未保存", "Grading settings have not been saved"),
      description: tx(
        locale,
        "任务的题目和作答仍然保留。请先保存本次批改设置，再从批改摘要确认启动。",
        "The task questions and submissions are preserved. Save the grading settings, then confirm the run from the grading summary.",
      ),
      actionLabel: tx(locale, "打开批改设置", "Open grading settings"),
      actionHref: taskHref("/grading-setup"),
      actionKind: "adjust_experts",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "questions_required") {
    return {
      title: tx(locale, "当前任务没有可批改题目", "This task has no questions to grade"),
      description: tx(
        locale,
        "请先补齐题目，再开始批改。",
        "Complete the questions before starting grading.",
      ),
      actionLabel: tx(locale, "返回上传题目", "Return to question upload"),
      actionHref: taskHref("/upload/problems"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "submissions_required") {
    return {
      title: tx(locale, "当前任务没有可批改作答", "This task has no submissions to grade"),
      description: tx(
        locale,
        "批改没有启动，因为当前任务缺少学生作答。请返回作答上传阶段添加作答。",
        "Grading did not start because the task has no student submissions. Return to submission upload to add them.",
      ),
      actionLabel: tx(locale, "返回上传作答", "Return to submission upload"),
      actionHref: taskHref("/submissions/upload"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "answers_required") {
    return {
      title: tx(locale, "学生作答尚未准备完整", "Student answers are not ready"),
      description: tx(
        locale,
        "部分学生的作答尚未准备好，请检查识别结果。",
        "Some student answers are not ready. Check the recognition results.",
      ),
      actionLabel: tx(locale, "检查学生作答", "Review student submissions"),
      actionHref: taskHref("/submissions"),
      actionKind: "reselect",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "submission_sources_pending") {
    return {
      title: tx(locale, "学生作答仍在识别", "Submission recognition is still running"),
      description: tx(
        locale,
        "批改没有启动，因为仍有作答来源正在处理。请查看识别进度，全部完成后再启动批改。",
        "Grading did not start because some submission sources are still processing. Check recognition progress and start grading after they finish.",
      ),
      actionLabel: tx(locale, "查看识别进度", "View recognition progress"),
      actionHref: taskHref("/submissions/progress"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "question_manual_completion_required") {
    return {
      title: tx(locale, "请先补全并确认题目", "Complete and confirm questions first"),
      description: tx(locale, "手动补全的题目仍有必填内容或尚未确认，批改没有启动。", "Some manually completed questions have missing materials or await confirmation. Grading has not started."),
      actionLabel: tx(locale, "补全题目资料", "Complete question materials"),
      actionHref: taskHref("/questions"), actionKind: "reselect", tone: "warning", technicalDetails,
    };
  }
  if (code === "saved_text_recovery_retired") {
    return {
      title: tx(locale, "此旧整理任务已停用", "This older organization task is retired"),
      description: tx(locale, "此任务来自已停用的旧流程，请新建任务并重新上传资料。", "This task uses a retired workflow. Create a new task and upload your materials again."),
      actionLabel: tx(locale, "新建任务", "Create a new task"),
      actionHref: "/tasks/new", actionKind: "reselect", retryUnavailable: true, tone: "warning", technicalDetails,
    };
  }
  if (code === "question_preparation_artifact_invalid" || code === "question_preparation_artifact_conflict") {
    return {
      title: tx(locale, "题目结果保存或恢复未完成", "Question results could not be saved or restored"),
      description: tx(locale, "已保存的识别内容仍保留。本次整理结果未能通过保存校验，可重试失败项或进入人工复核。", "Saved recognition content is retained. The organized result could not pass the save checks. Retry failed items or continue to manual review."),
      actionLabel: tx(locale, "重试失败项", "Retry failed items"),
      actionKind: "retry", tone: "danger", technicalDetails,
    };
  }
  if (code === "question_preparation_manual_unavailable") {
    return {
      title: tx(locale, "当前资料无法转入手动补全", "Manual completion is unavailable"),
      description: tx(locale, "尚未保存可核对的题目草稿，或任务状态已改变。请刷新状态；仍未识别出题目时可重试识别或返回修改资料。", "No verified question draft is available, or the task has changed. Refresh its status, retry recognition, or update the source materials."),
      actionLabel: tx(locale, "返回题目资料", "Return to question materials"),
      actionHref: taskHref("/upload/problems"), actionKind: "reselect", tone: "warning", technicalDetails,
    };
  }
  if (code === "submission_sources_failed") {
    return {
      title: tx(locale, "部分学生作答识别失败", "Some submissions failed recognition"),
      description: tx(
        locale,
        "请先处理识别失败的文件。",
        "Resolve the files that failed recognition first.",
      ),
      actionLabel: tx(locale, "查看识别失败", "Review recognition failures"),
      actionHref: taskHref("/submissions/progress"),
      actionKind: "reselect",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_identities_unresolved") {
    return {
      title: tx(locale, "仍有学生身份待确认", "Some student identities are unresolved"),
      description: tx(
        locale,
        "请核对并确认学生身份。",
        "Check and confirm the student identities.",
      ),
      actionLabel: tx(locale, "校对学生身份", "Review student identities"),
      actionHref: taskHref("/submissions"),
      actionKind: "reselect",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "submission_recognition_blocked") {
    return {
      title: tx(locale, "作答仍有关键问题待处理", "Some answers need correction"),
      description: tx(locale, "请补齐识别失败的作答后继续。学生真实留白和普通低置信提醒不会阻止批改。", "Repair failed transcriptions before continuing. Genuine blank responses and ordinary confidence warnings do not block grading."),
      actionLabel: tx(locale, "校对作答", "Review submissions"),
      actionHref: taskHref("/submissions"),
      actionKind: "reselect", tone: "danger", technicalDetails,
    };
  }

  if (code === "submission_recognition_needs_review") {
    return {
      title: tx(locale, "仍有作答识别待核对", "Some transcriptions need review"),
      description: tx(locale, "请对照原件核对标记的作答，确认后继续。", "Check the flagged answers against the originals and confirm before continuing."),
      actionLabel: tx(locale, "核对作答", "Review submissions"),
      actionHref: taskHref("/submissions?filter=review"),
      actionKind: "reselect", tone: "warning", technicalDetails,
    };
  }

  if (code === "submission_source_evidence_missing") {
    return {
      title: tx(locale, "作答状态需要重新确认", "The submission state needs confirmation"),
      description: tx(
        locale,
        "文件已保存，请刷新任务状态后重试。",
        "Your files are saved. Refresh the task and try again.",
      ),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "workflow_busy" || normalized.includes("workflow_busy")) {
    return {
      title: tx(locale, "任务仍在处理", "The task is still processing"),
      description: tx(
        locale,
        "另一阶段正在运行，请等待完成。",
        "Another stage is running. Wait for it to finish.",
      ),
      actionLabel: tx(locale, "刷新任务状态", "Refresh task state"),
      actionKind: "refresh",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "already_running" || normalized.includes("already_running")) {
    return {
      title: tx(locale, "批改已经在运行", "Grading is already running"),
      description: tx(
        locale,
        "已有批改正在运行，请刷新查看。",
        "A grading run already exists. Refresh to view it.",
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
        "批改结果未能完整保存，请稍后重试；若仍失败，请联系管理员。",
        "Grading results could not be fully saved. Try again later or contact your administrator if it continues.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "programming_execution_unavailable") {
    return {
      title: tx(locale, "编程题测试暂不可用", "Programming tests are unavailable"),
      description: tx(locale,
        "编程题测试未能运行，已有结果已保留。请联系管理员后重试失败项，或人工补齐分数。",
        "Programming tests could not run. Existing results are retained. Contact your administrator, then retry failed items or enter scores manually."),
      actionLabel: tx(locale, "查看进度与恢复", "View progress and recovery"),
      actionKind: "refresh",
      actionHref: taskId ? `/tasks/${taskId}/grading/progress` : undefined,
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "grading_failed") {
    return {
      title: tx(locale, "本次批改没有完成", "This grading run did not finish"),
      description: tx(
        locale,
        "批改未完成，任务资料已保留。请稍后重试；若仍失败，请联系管理员。",
        "Grading did not finish. Task data is retained. Retry later or contact your administrator if it continues.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "provider_capacity_unavailable") {
    return {
      title: tx(locale, "模型调用正在排队", "Model capacity is busy"),
      description: tx(locale,
        "SmarTAI 未能在限定时间内取得调用名额，尚未向模型发送本次请求。任务资料已保留，请稍后重试；持续出现时请联系管理员检查服务器资源。",
        "SmarTAI could not obtain capacity before the deadline; this request was not sent to the model. Your task data is preserved. Retry later; if this persists, ask an administrator to check server resources."),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_timeout") {
    return {
      title: tx(locale, "模型响应超时", "The model took too long to respond"),
      description: tx(
        locale,
        "模型响应超时，请稍后重试。",
        "The model timed out. Please try again later.",
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
        "无法连接模型服务，请检查模型配置后重试。",
        "Cannot connect to the model service. Check the model configuration and try again.",
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
        "请在“模型与 BYOK”检查 API Key、模型权限和可用额度后重试。",
        "Check the API key, model access, and available quota in Models & BYOK, then retry.",
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
      title: tx(locale, "当前识别模型不能读取图片/扫描件", "The current recognition model cannot read images or scans"),
      description: tx(
        locale,
        "当前阶段选择的模型拒绝了图片输入。原文件和已完成步骤均已保留；请改选识别模型后重试。",
        "The model selected for this stage rejected image input. The original file and completed steps are preserved; switch the recognition model and retry.",
      ),
      actionLabel: tx(locale, "更换识别模型后重试", "Switch recognition model and retry"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_recitation_blocked" || code === "provider_content_blocked") {
    const observed = Array.isArray(context.providerFailures) ? context.providerFailures.at(-1) : undefined;
    const pages = safeFailurePages(observed?.code === code ? observed.pages : undefined);
    if (!pages.length) pages.push(...safeFailurePages(detail?.failed_pages));
    const pageHint = pages.length ? tx(locale, `失败位置：PDF 第 ${pages.join("、")} 页。`, `Affected PDF pages: ${pages.join(", ")}. `) : "";
    return {
      title: code === "provider_recitation_blocked"
        ? tx(locale, "模型停止了原文转写", "The model stopped transcribing this content")
        : tx(locale, "模型限制了这次内容输出", "The model blocked this content output"),
      description: pageHint + (code === "provider_recitation_blocked"
        ? tx(locale, "服务商将输出判定为可能复述受版权保护的原文（RECITATION），因此没有返回识别结果。这不代表模型缺少图片能力。请确认题号或页码只包含所需内容，或更换识别模型后重试；原文件和填写内容仍保留。", "The provider flagged the output as possible recitation of copyrighted text (RECITATION) and returned no transcription. This does not mean the model lacks image support. Check that the target questions or pages match what you need, or change the recognition model. Your file and input are preserved.")
        : tx(locale, "服务商已接收请求，但因内容策略停止输出。这不代表图片测试失败。请调整所需识别范围或更换模型；原文件和填写内容仍保留。", "The provider received the request but stopped output under its content policy. This is not an image-test failure. Adjust the requested scope or change models; your file and input are preserved.")),
      actionLabel: tx(locale, "更换模型", "Change model"),
      actionHref: `/settings/byok${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`,
      actionKind: "byok", tone: "warning", technicalDetails,
    };
  }

  if (code === "recognition_response_invalid") {
    const observed = Array.isArray(context.providerFailures) ? context.providerFailures.at(-1) : undefined;
    const pages = safeFailurePages(observed?.code === code ? observed.pages : undefined);
    if (!pages.length) pages.push(...safeFailurePages(detail?.failed_pages));
    const cause = uncertainProviderCauseCopy({
      code, pages,
    }, locale)!;
    return {
      title: cause.title, description: cause.description,
      actionLabel: tx(locale, "重新尝试", "Try again"), actionKind: "retry",
      tone: "warning", technicalDetails,
    };
  }

  const providerConfigurationCopy = providerConfigurationErrorCopy(code, locale);
  if (providerConfigurationCopy) {
    return {
      ...providerConfigurationCopy,
      actionLabel: tx(locale, "检查 BYOK 配置", "Check BYOK settings"),
      actionHref: `/settings/byok${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`,
      actionKind: "byok",
      tone: "primary",
      technicalDetails,
    };
  }

  const providerTransientCopy = providerTransientErrorCopy(code, locale);
  if (providerTransientCopy) {
    return {
      ...providerTransientCopy,
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "provider_image_payload_invalid") {
    return {
      title: tx(locale, "图片无法读取", "The image could not be read"),
      description: tx(
        locale,
        "图片无法读取，请重新上传清晰的 JPG、PNG 或 WebP。",
        "The image could not be read. Upload a clear JPG, PNG, or WebP image.",
      ),
      actionLabel: tx(locale, "重新选择文件", "Choose the file again"),
      actionKind: "reupload",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "vision_provider_required" || code === "visual_capability_unavailable") {
    const byokReturnTo = context.returnTo?.trim();
    return {
      title: tx(locale, "尚未选择可用的视觉模型", "No usable vision model is selected"),
      description: tx(
        locale,
        "请选用支持图片识别的模型，或上传可复制文字版文件。",
        "Select a model that supports image recognition, or upload a text-based file.",
      ),
      actionLabel: tx(locale, "选择支持 OCR 的模型", "Choose an OCR-capable model"),
      actionHref: `/settings/byok${byokReturnTo ? `?returnTo=${encodeURIComponent(byokReturnTo)}` : ""}`,
      actionKind: "byok",
      tone: "primary",
      technicalDetails,
    };
  }

  if (code === "question_targets_incomplete") {
    return {
      title: tx(locale, "未找齐指定题目", "Some requested questions were not found"),
      description: tx(locale, "请核对题号和页码后重试；扫描件或复杂公式可改用图片识别模型。已上传资料仍保留。", "Check question and page numbers, then retry. For scans or complex formulas, try an image-capable model. Your uploaded sources are saved."),
      actionLabel: tx(locale, "检查题号与页码", "Check question and page numbers"),
      actionKind: "retry", tone: "warning", technicalDetails,
    };
  }
  if (["target_location_needs_hint", "target_selection_limit_exceeded", "recognition_budget_exhausted", "recognition_timeout"].includes(code ?? "")) {
    return {
      title: tx(locale, "本次识别范围尚未完成", "Recognition coverage is incomplete"),
      description: tx(locale, "已保存的识别结果仍然保留。请缩小页码范围或补充目标题号；系统不会自动增加付费调用。", "Saved evidence is retained. Narrow the page range or specify question numbers; no extra paid calls are started automatically."),
      actionLabel: tx(locale, "调整识别范围", "Adjust source range"),
      actionKind: "retry", tone: "warning", technicalDetails,
    };
  }
  if (code === "ocr_empty_result" || code === "image_recognition_unconfirmed") {
    return {
      title: tx(locale, "OCR 没有读到可用文字", "OCR found no usable text"),
      description: tx(
        locale,
        "图片识别未得到可靠文字，可能来自文件模糊或模型识别效果，不能据此判定模型不支持图片。请换清晰文件或换模型；图片能力未确认时也可在模型配置页主动验证。",
        "Image recognition did not yield reliable text. Blurry input or model accuracy may cause this; it does not prove lack of image support. Try a clearer file or another model; if capability is uncertain, verify it in model settings.",
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
        "识别出的题号与当前任务不匹配，请核对文件和题号。",
        "Recognized question numbers do not match this task. Check the file and question numbers.",
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
        "未找到作答内容，请检查空白页、封面或裁剪范围。",
        "No answers were found. Check for blank pages, cover pages, or cropped content.",
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
        "原文件未能保存，请重试；若仍失败，请联系管理员。",
        "The original file could not be saved. Retry or contact your administrator if it continues.",
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
        "作答结果未能保存，请联系管理员。原文件和已完成识别已保留。",
        "Recognized answers could not be saved. Contact your administrator. Originals and completed recognition are retained.",
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
        "识别结果未完整保存，任务已暂停。原文件仍保留，请重试；若再次失败，请提供任务编号联系管理员。",
        "Some recognition results could not be saved, so the task paused. The originals are retained. Retry; if it fails again, contact an administrator with the task ID.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "question_structure_score_mismatch") {
    return {
      title: tx(locale, "题号或分项分值与教师设置不一致", "Question numbers or points differ from teacher instructions"),
      description: tx(locale,
        "题号或分值不一致，请核对后重新准备。",
        "Question numbers or scores do not match. Check them before preparing the questions again.",
      ),
      actionLabel: tx(locale, "重新准备题目", "Prepare questions again"),
      actionHref: taskId ? `/tasks/${taskId}/upload/problems` : undefined,
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "submission_parse_invalid") {
    return {
      title: tx(locale, "模型返回格式无法解析", "The model returned an invalid structure"),
      description: tx(
        locale,
        "未能完整识别学生信息与作答，请重试或更换模型。",
        "Could not recognize all student details and answers. Retry or choose another model.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
      technicalDetails,
    };
  }

  if (code === "submission_model_field_too_long") {
    return {
      title: tx(locale, "识别结果格式有误", "Invalid recognition result"),
      description: tx(
        locale,
        "识别结果格式有误，请重试或更换模型。",
        "The recognition result has an invalid format. Retry or choose another model.",
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
        ? tx(locale, "原文件已保存，请稍后重试。", "The original is saved. Please try again later.")
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

  if (code === "source_storage_quota_exceeded") {
    return {
      title: tx(locale, "原文件空间已满", "Original-file storage is full"),
      description: tx(
        locale,
        "本次文件未保存。请稍后重试，或选择更小的文件。",
        "This file was not saved. Try again later or choose a smaller file.",
      ),
      actionLabel: tx(locale, "选择更小文件", "Choose a smaller file"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (isKnowledgeStorageQuotaExceeded(apiError)) {
    const copy = knowledgeStorageQuotaCopy(locale);
    return {
      title: copy.title,
      description: copy.description,
      actionLabel: tx(locale, "调整上传资料", "Review upload"),
      actionKind: "reupload",
      tone: "warning",
      technicalDetails,
    };
  }

  if (code === "operation_cancelled") {
    return { title: tx(locale, "本次运行已停止", "This run has stopped"),
      description: tx(locale, "按原配置继续未完成部分，保留已保存结果，并按新的提交时间重新排队。未完成的模型请求可能再次计费。", "Continue unfinished work with saved settings and results, rejoining the queue with a new submission time. Unfinished model requests may be billed again."),
      actionLabel: tx(locale, "继续处理", "Continue processing"), actionKind: "retry", tone: "warning", technicalDetails };
  }
  if (code === "provider_quota_exceeded") {
    return { title: tx(locale, "模型余额或可用额度已耗尽", "Model credits or quota exhausted"),
      description: tx(locale, "本轮已停止，不会自动重试。请检查服务商余额与额度，恢复后主动重试，或切换模型。", "This run has stopped without automatic retries. Check provider credits and limits, then retry or switch models."),
      actionLabel: tx(locale, "查看模型配置", "Model settings"), actionHref: "/settings/models", actionKind: "byok",
      tone: "warning", showUsageLinks: true, technicalDetails };
  }
  if (code === "provider_daily_quota_exceeded" || code === "shared_pool_daily_limit_reached") {
    return {
      title: tx(locale, "模型日额度已用完", "The model's daily quota is exhausted"),
      description: tx(locale, "模型日额度已用完，本轮已停止。额度恢复后重新启动，或更换模型。", "The model’s daily quota is exhausted and this run has stopped. Restart once quota is available, or choose another model."),
      actionLabel: tx(locale, "查看模型配置", "Model settings"), actionHref: "/settings/models", actionKind: "byok", tone: "warning",
      dailyQuotaExhausted: true, showUsageLinks: true, technicalDetails,
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
        `服务商仍返回限流，本轮已停止，自动尝试最多 3 次。当前信息不足以确定是分钟限制还是日额度。可查看官网用量、切换模型，或按原配置重新排队；成功部分保留。${wait}`,
        `The provider still reports a limit. This run has stopped after at most 3 automatic attempts. The response does not identify a minute or daily limit. Check usage, switch models, or rejoin the queue with the same settings; saved results are kept.${wait}`,
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      retryAfterSeconds,
      showUsageLinks: true,
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
        "请重新选择或上传资料，再开始识别。",
        "Select or upload the material again, then restart recognition.",
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
    isWorkflowRevisionConflictCode(code)
    || normalized.includes("stale_revision")
    || normalized.includes("workflow_revision_conflict")
  ) {
    return {
      title: tx(locale, "任务状态已经变化", "The task state has changed"),
      description: friendlyMessage(message, code, tx(locale, "请刷新任务状态后继续。", "Refresh the task status to continue.")),
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
      title: tx(locale, "暂时无法连接服务", "Cannot connect to the service"),
      description: message.includes("waking up")
        ? tx(locale, "暂时无法连接服务，请稍后重试。", "Cannot connect to the service. Please try again later.")
        : friendlyMessage(message, code, tx(locale, "请检查网络后重试。", "Check your connection and try again.")),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "warning",
      technicalDetails,
    };
  }

  return {
    title: apiError.status >= 500
      ? tx(locale, "本次操作未完成", "This action did not complete")
      : tx(locale, "本次操作未完成", "This action did not complete"),
    description: friendlyMessage(
      message,
      code,
      tx(
        locale,
        "本次操作未完成，请稍后重试；若仍失败，请联系管理员。",
        "This action did not complete. Try again later or contact your administrator if it continues.",
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
    { label: tx(locale, "原文件已用", "Original storage used"), value: formatByteLimit(detail?.used_bytes) },
    { label: tx(locale, "原文件额度", "Original storage limit"), value: formatByteLimit(detail?.limit_bytes) },
    { label: tx(locale, "本次上传", "Requested upload"), value: formatByteLimit(detail?.requested_bytes) },
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

function providerConfigurationErrorCopy(
  code: string | null,
  locale: Locale,
): Pick<RecoverableErrorInfo, "title" | "description"> | null {
  const copies: Record<string, [string, string, string, string]> = {
    provider_model_not_found: [
      "模型名称不可用",
      "The model name is unavailable",
      "服务商找不到当前模型，或当前 API Key 无权使用它。请核对模型名称或改选当前阶段的模型。",
      "The provider could not find this model, or the current API key cannot access it. Check the model name or switch the model for this stage.",
    ],
    provider_model_or_endpoint_not_found: [
      "模型名称或接口路径不存在",
      "The model or endpoint was not found",
      "请核对模型名称和 Base URL。中转站应填写服务根路径，不要填写完整操作终点。",
      "Check the model name and base URL. Enter the relay service root, not a full operation endpoint.",
    ],
    provider_request_rejected: [
      "模型接口未接受本次请求",
      "The model API did not accept this request",
      "服务商认为本次请求的参数或格式不符合接口要求，当前信息不足以确定具体参数。这不能证明模型不支持图片。请核对模型名称、接口地址和 API 协议，或换模型重试；原文件和填写内容仍保留。",
      "The provider rejected the request parameters or format; the exact parameter is not known. This does not establish a lack of image support. Check the model name, endpoint and API protocol, or try another model. Your file and input are preserved.",
    ],
    provider_response_invalid: [
      "模型服务返回了无法识别的响应",
      "The model service returned an invalid response",
      "请重试未完成步骤，或更换模型。原文件和已完成步骤已保留。",
      "Retry the unfinished step or change models. Your files and completed steps are saved.",
    ],
    ocr_credential_not_found: [
      "所选 OCR 凭据已不存在",
      "The selected OCR credential no longer exists",
      "返回模型与 BYOK 页面重新保存百度 OCR 的 API Key / Secret Key，然后重新选择识别服务。",
      "Return to Models & BYOK, save the Baidu OCR API Key / Secret Key again, then reselect the recognition service.",
    ],
    provider_permission_denied: [
      "OCR 账号没有服务权限",
      "The OCR account lacks service permission",
      "请在百度控制台确认已开通文档解析权限，或替换为有权限的 API Key / Secret Key。",
      "Confirm Document Parsing access in the Baidu console or replace the API Key / Secret Key with an authorized pair.",
    ],
    provider_region_unsupported: [
      "模型服务不支持当前网络地区",
      "The model service does not support the current network region",
      "请使用供应商支持的接入环境，或在 BYOK 中选择其他已获授权的模型。剩余页面不会自动重复请求。",
      "Use a provider-supported access environment or select another authorized BYOK model. Remaining pages are not retried automatically.",
    ],
    shared_pool_daily_limit_reached: ["共享模型日额度已用完。", "Shared model daily allowance exhausted.", "请等待 UTC 00:00 重置、联系管理员调整，或在模型与 BYOK 页面配置自己的模型。", "Wait for UTC midnight, contact an administrator, or use your own BYOK model."],
    shared_pool_disabled: ["平台共享模型当前关闭。", "The shared model pool is disabled.", "请在模型与 BYOK 页面配置自己的模型。", "Configure your own BYOK model."],
    provider_quota_exceeded: [
      "OCR 额度已用完",
      "The OCR quota is exhausted",
      "请在百度控制台检查当前活动额度与用量，额度恢复后再重试。",
      "Check the current campaign quota and usage in the Baidu console, then retry after quota is available.",
    ],
    provider_message_payload_not_supported: [
      "当前协议无法发送这类输入",
      "The selected protocol cannot encode this input",
      "请核对高级 API 协议，或改用支持当前文件类型的模型。",
      "Check the Advanced API protocol or choose a model that supports this file type.",
    ],
    provider_endpoint_dns_failed: [
      "中转站域名无法解析",
      "The relay hostname could not be resolved",
      "请检查 Base URL 拼写与服务状态。",
      "Check the Base URL and service status.",
    ],
    provider_endpoint_non_public_address: [
      "中转站域名解析到非公网地址",
      "The relay hostname resolved to a non-public address",
      "请检查中转站 DNS 配置；系统已阻止访问内网或特殊地址。",
      "Check the relay DNS configuration. Access to private or special addresses was blocked.",
    ],
    provider_endpoint_tls_failed: [
      "中转站 HTTPS 证书校验失败",
      "The relay HTTPS certificate could not be verified",
      "请让服务提供方修复证书后重试。",
      "Ask the service provider to fix its certificate before retrying.",
    ],
    provider_endpoint_redirect_blocked: [
      "中转站返回了重定向",
      "The relay returned a redirect",
      "请将 Base URL 改为最终的 HTTPS 服务地址。",
      "Set the Base URL to the final HTTPS service address.",
    ],
    provider_endpoint_protocol_mismatch: [
      "中转站响应与所选 API 协议不一致",
      "The relay response does not match the selected API protocol",
      "请在 BYOK 高级设置中核对 OpenAI、Anthropic 或 Gemini 协议。",
      "Check the OpenAI, Anthropic, or Gemini protocol in BYOK Advanced settings.",
    ],
  };
  const copy = code ? copies[code] : undefined;
  return copy
    ? { title: tx(locale, copy[0], copy[1]), description: tx(locale, copy[2], copy[3]) }
    : null;
}

function safeFailurePages(value: unknown): number[] {
  return Array.isArray(value)
    ? [...new Set(value.filter((page): page is number => Number.isInteger(page) && page > 0 && page <= 10000))].slice(0, 24)
    : [];
}

function uncertainProviderCauseCopy(failure: ProviderFailure, locale: Locale) {
  const copies: Record<string, [string, string, string, string]> = {
    provider_upstream_unavailable: ["模型服务暂时不可用", "The model service is temporarily unavailable", "本次运行最近记录到服务商返回临时故障", "The latest recorded model failure in this run was a temporary provider error"],
    provider_timeout: ["模型响应超时", "The model response timed out", "本次运行最近记录到模型响应超时，未能在等待时间内取得结果", "The latest recorded model failure in this run was a response timeout"],
    provider_unreachable: ["模型连接失败或中断", "The model connection failed or was interrupted", "本次运行最近记录到模型连接失败或中断，未能取得完整结果", "The latest recorded model failure in this run was a failed or interrupted connection"],
    provider_rate_limited: ["模型请求受到限流", "The model request was rate limited", "本次运行最近记录到服务商限流", "The latest recorded model failure in this run was provider rate limiting"],
    provider_response_invalid: ["模型返回结果无法使用", "The model response could not be used", "本次运行最近记录到模型返回结果未通过格式校验", "The latest recorded model failure in this run was an invalid response format"],
    recognition_response_invalid: ["识别结果无法使用", "The recognition response could not be used", "返回的识别结果未通过有效性校验，无法作为已完成结果使用", "The recognition response failed validation and could not be accepted as completed"],
    provider_recitation_blocked: ["模型停止了原文转写", "The model stopped transcribing this content", "服务商将输出判定为可能复述受版权保护的原文（RECITATION），停止返回识别结果。请调整所需识别范围或更换模型", "The provider flagged possible recitation of copyrighted text (RECITATION) and stopped transcription. Adjust the requested scope or change models"],
    provider_content_blocked: ["模型限制了这次内容输出", "The model blocked this content output", "服务商因内容策略停止输出识别结果。请调整所需识别范围或更换模型", "The provider stopped transcription under its content policy. Adjust the requested scope or change models"],
  };
  if (typeof failure.code !== "string" || !Object.hasOwn(copies, failure.code)) return null;
  const copy = copies[failure.code];
  const status = failure.http_status;
  const httpStatus = typeof status === "number" && Number.isInteger(status) && (
    (failure.code === "provider_upstream_unavailable" && status >= 500 && status <= 599)
    || (failure.code === "provider_rate_limited" && status === 429)
  ) ? status : null;
  const pages = safeFailurePages(failure.pages);
  const pageHint = pages.length ? tx(locale, `失败位置：PDF 第 ${pages.join("、")} 页。`, `Affected PDF pages: ${pages.join(", ")}. `) : "";
  return {
    title: tx(locale, copy[0], copy[1]),
    description: pageHint + tx(locale, copy[2], copy[3]) + (httpStatus ? ` (HTTP ${httpStatus})` : "") + tx(locale, "。", "."),
    httpStatus,
  };
}

function providerTransientErrorCopy(
  code: string | null,
  locale: Locale,
): Pick<RecoverableErrorInfo, "title" | "description"> | null {
  if (code === "provider_overloaded") {
    return {
      title: tx(locale, "模型服务当前拥堵", "The model service is busy"),
      description: tx(locale, "供应商暂时无法接收请求，并非已确认的每日额度耗尽。资料已保留，请稍后重试或选择另一个已启用模型。", "The provider is temporarily overloaded; this does not confirm a daily quota limit. Your files are preserved. Retry later or select another enabled model."),
    };
  }
  if (code === "grading_provider_transient") {
    return {
      title: tx(locale, "部分作答遇到模型服务临时故障", "Some responses could not be graded due to a temporary model-service failure"),
      description: tx(locale, "模型服务暂时不可用、连接失败或请求超时。已保存的成功结果会保留，请仅重试失败项；若反复失败，可在批改设置中检查模型。", "The model service was unavailable, the connection failed, or the request timed out. Saved successful results are retained. Retry only the failed items; if failures persist, check the model in grading setup."),
    };
  }
  if (code === "provider_upstream_unavailable") {
    return {
      title: tx(locale, "模型服务暂时不可用", "The model service is temporarily unavailable"),
      description: tx(locale, "模型服务返回了临时故障。任务资料已保留，请稍后重试或换用另一个已启用模型。", "The model service reported a temporary failure. Task data is preserved; retry later or use another enabled model."),
    };
  }
  if (code === "provider_unavailable" || code === "provider_task_failed") {
    return {
      title: tx(locale, "OCR 服务暂时不可用", "The OCR service is temporarily unavailable"),
      description: tx(locale, "原文件已保留，请稍后重试。", "The original is preserved. Please retry later."),
    };
  }
  if (code === "provider_endpoint_response_too_large") {
    return {
      title: tx(locale, "返回内容过大", "The response is too large"),
      description: tx(locale, "返回内容过大，请缩小输入或输出范围后重试。", "The response is too large. Reduce the input or requested output and try again."),
    };
  }
  return null;
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
  if (["source_empty", "submission_source_empty"].includes(code ?? "") || /file is empty|contains no usable text|no extractable text/i.test(message)) {
    return tx(locale, "文件为空，请确认内容后重新保存并上传。", "The file is empty. Check its contents, save it, and upload again.");
  }
  if (code === "source_content_type_not_allowed" || code === "submission_source_content_type_mismatch") {
    return tx(locale, "文件内容与扩展名不符，或文件已损坏。请确认能在本地打开，并重新导出为上传区域支持的格式；只修改文件后缀无效。", "The file contents do not match its extension, or the file is damaged. Check that it opens locally and export it in a format supported by this upload area; renaming the extension does not convert it.");
  }
  if (code === "pdf_encrypted") {
    return tx(locale, "PDF 已加密或需要密码。请在本地解锁并另存为无需密码的 PDF 后上传。", "This PDF is encrypted or password-protected. Unlock it locally and save a PDF that opens without a password before uploading.");
  }
  if (["pdf_invalid", "pdf_extraction_failed"].includes(code ?? "")) {
    return tx(locale, "PDF 损坏或文件内容并非 PDF。请确认能在本地打开，再重新导出为 PDF；只修改扩展名无效。", "The PDF is damaged or its contents are not a PDF. Check that it opens locally and export it as PDF again; renaming the extension does not convert it.");
  }
  if (code === "pdf_page_out_of_range") {
    return tx(locale, "所选页码超出 PDF 范围。请使用文件页序号，检查起止页后重试。", "The selected page is outside the PDF. Check the start and end positions using the file's page order, then retry.");
  }
  if (["image_invalid", "image_format_mismatch", "image_mode_unsupported", "image_multiframe_unsupported"].includes(code ?? "")) {
    return tx(locale, "图片损坏、内容与扩展名不符，或使用了不支持的图片模式。请确认能在本地打开，重新导出为单张 JPG 或 PNG 后上传；不要只修改扩展名。", "The image is damaged, its contents do not match its extension, or its image mode is unsupported. Check that it opens locally and export it as a single JPG or PNG; do not just rename the extension.");
  }
  if (code === "image_pixel_limit_exceeded") {
    return tx(locale, "图片像素尺寸超过处理上限。请缩小图片尺寸并保留文字清晰度，再上传。", "The image dimensions exceed the processing limit. Reduce its pixel dimensions while keeping the text readable, then upload again.");
  }
  if (code === "source_too_large") {
    const limit = formatByteLimit(detail?.max_bytes);
    return limit
      ? tx(locale, `文件超过 ${limit} 的单文件上传上限，请选择更小的文件。`, `The file exceeds the ${limit} per-file upload limit. Choose a smaller file.`)
      : tx(locale, "文件超过单文件上传上限，请选择更小的文件。", "The file exceeds the per-file upload limit. Choose a smaller file.");
  }
  if (code === "problem_source_decode_failed" || code === "source_decode_failed") {
    return tx(locale, "没有从文件中读取到可用正文。若是扫描 PDF，请先转换为可复制文字的 PDF、TXT 或 Markdown。", "No usable text could be read. If this is a scanned PDF, convert it to a text-based PDF, TXT, or Markdown file first.");
  }
  if (code === "ocr_empty_result" || code === "image_recognition_unconfirmed") {
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

function friendlyMessage(_message: string, _code: string | null, fallback: string): string {
  // Raw provider/server text is not display copy. Keep classification and
  // recovery based on the original error, then use the localized description.
  return fallback;
}

/** Display text only; callers retain the original error for status and recovery. */
export function userFacingError(error: unknown, locale: string = "zh-CN"): string {
  return classifyRecoverableError(error, { locale: locale === "en-US" ? "en-US" : "zh-CN" }).description;
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

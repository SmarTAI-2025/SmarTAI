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
  "recognition_provider_not_enabled",
  "provider_not_enabled",
  "vision_provider_required",
  "shared_pool_kb_requires_byok",
  "no_enabled_expert",
  "expert_verification_auth_failed",
]);

const FILE_CODES = new Set([
  "source_type_not_allowed",
  "source_mime_type_not_allowed",
  "problem_source_unsupported",
  "problem_source_decode_failed",
  "problem_source_character_limit_exceeded",
  "problem_source_token_limit_exceeded",
  "pdf_page_limit_exceeded",
  "pdf_character_limit_exceeded",
  "submission_source_unsupported",
  "submission_source_empty",
  "submission_source_too_large",
  "submission_roster_unsupported",
  "submission_roster_empty",
  "submission_roster_too_large",
  "submission_roster_too_many_rows",
  "submission_roster_headers_invalid",
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

  if (code === "grading_failed") {
    return {
      title: tx(locale, "本次批改没有完成", "This grading run did not finish"),
      description: tx(
        locale,
        "后端未能完成批改或保存结果。任务资料仍然保留；请记录任务编号，处理后再重试。",
        "The backend could not complete grading or save its results. Task data is preserved; keep the job ID and retry after the issue is resolved.",
      ),
      actionLabel: tx(locale, "重新尝试", "Try again"),
      actionKind: "retry",
      tone: "danger",
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
    const returnTo = context.returnTo?.trim();
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
      description: fileErrorDescription(code, message, locale),
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
    description: friendlyMessage(message, code, tx(locale, "可以重试；当前任务内容不会丢失。", "You can retry; the current task content is preserved.")),
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
    { label: tx(locale, "文件上限", "File-size limit"), value: safeTechnicalValue(detail?.max_bytes) },
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

function fileErrorDescription(code: string | null, message: string, locale: Locale): string {
  if (code === "problem_source_decode_failed") {
    return tx(locale, "没有从文件中读取到可用正文。若是扫描 PDF，请先转换为可复制文字的 PDF、TXT 或 Markdown。", "No usable text could be read. If this is a scanned PDF, convert it to a text-based PDF, TXT, or Markdown file first.");
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

import { FileUp, LoaderCircle } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { useExperts, useParseSubmissions, useRetrySubmissionRecognition, useTask } from "@/api/hooks";
import { StageProviderSelect } from "@/components/models/StageProviderSelect";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { getTaskDestination, hasTaskReachedStep } from "@/lib/taskFlow";
import type { SubmissionIdentityMode } from "@/types";

const SUBMISSION_SUFFIXES = [
  ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
  ".txt", ".md", ".rst", ".csv", ".pdf", ".jpg", ".jpeg", ".png", ".webp",
] as const;
const ROSTER_SUFFIXES = [".csv", ".tsv", ".txt"] as const;

const IDENTITY_OPTIONS: Array<{ mode: SubmissionIdentityMode; label: MessageKey }> = [
  { mode: "filename", label: "submissionUploadIdentityFilename" },
  { mode: "roster", label: "submissionUploadIdentityRoster" },
  { mode: "manual_review", label: "submissionUploadIdentityManual" },
];

type SubmissionDraft = {
  selectedFile: File | null;
  rosterFile: File | null;
  identityMode: SubmissionIdentityMode;
  recognitionProviderId: string;
};

const submissionDrafts = new Map<string, SubmissionDraft>();

export function AddSubmissionsPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const { t, locale } = useI18n();
  const taskQuery = useTask(taskId);
  const expertsQuery = useExperts();
  const parseSubmissions = useParseSubmissions();
  const retryRecognition = useRetrySubmissionRecognition();
  const submissionInputRef = useRef<HTMLInputElement>(null);
  const rosterInputRef = useRef<HTMLInputElement>(null);

  const savedDraft = taskId ? submissionDrafts.get(taskId) : undefined;
  const [selectedFile, setSelectedFile] = useState<File | null>(savedDraft?.selectedFile ?? null);
  const [rosterFile, setRosterFile] = useState<File | null>(savedDraft?.rosterFile ?? null);
  const [identityMode, setIdentityMode] = useState<SubmissionIdentityMode>(savedDraft?.identityMode ?? "filename");
  const [recognitionProviderId, setRecognitionProviderId] = useState(
    savedDraft?.recognitionProviderId ?? "",
  );
  const [isDragging, setIsDragging] = useState(false);
  const [uploadPercent, setUploadPercent] = useState(0);
  const [formError, setFormError] = useState<string | null>(null);
  const [needsModel, setNeedsModel] = useState(false);

  const task = taskQuery.data;
  const enabledExperts = (expertsQuery.data ?? []).filter((expert) => expert.enabled);
  const hasExistingSubmissions = Boolean(
    task?.submission_file_name
      || task?.pending_submission_file_name
      || task?.student_count,
  );
  const needsReplacementConfirmation = Boolean(
    task?.submission_file_name || task?.student_count,
  );
  const isRecognitionRunning = task?.status === "parsing_submissions";
  const isWorkflowBusy = task?.status === "extracting_problems";
  const canRetryOriginal = Boolean(
    task?.status === "error"
      && task.last_failed_job_id
      && task.pending_submission_file_name
      && !selectedFile,
  );
  const isPending = parseSubmissions.isPending || retryRecognition.isPending;
  const visibleFileName = selectedFile?.name ?? (
    canRetryOriginal ? task?.pending_submission_file_name ?? null : null
  );

  useEffect(() => {
    if (!taskId) return;
    submissionDrafts.set(taskId, {
      selectedFile,
      rosterFile,
      identityMode,
      recognitionProviderId,
    });
  }, [identityMode, recognitionProviderId, rosterFile, selectedFile, taskId]);

  useEffect(() => {
    if (expertsQuery.isLoading || expertsQuery.isError) return;
    const enabled = (expertsQuery.data ?? []).filter((expert) => expert.enabled);
    setRecognitionProviderId((current) => {
      if (enabled.some((expert) => expert.provider_id === current)) return current;
      const frozenProviderId = task?.submission_recognition_provider_id;
      if (
        frozenProviderId
        && enabled.some((expert) => expert.provider_id === frozenProviderId)
      ) {
        return frozenProviderId;
      }
      return (
        enabled.find((expert) => expert.is_default)?.provider_id
        ?? enabled[0]?.provider_id
        ?? ""
      );
    });
  }, [expertsQuery.data, expertsQuery.isError, expertsQuery.isLoading, task?.submission_recognition_provider_id]);

  const uploadDisabledReason = isRecognitionRunning
    ? null
    : taskQuery.isLoading
      ? t("submissionUploadTaskLoading")
      : taskQuery.isError || !task
        ? t("submissionUploadTaskUnavailable")
        : isWorkflowBusy
          ? task.status === "grading"
            ? t("submissionUploadGradingLocked")
            : t("submissionUploadBusy")
          : !recognitionProviderId
            ? localText(locale, "需要先添加或选择一个已启用模型。", "Add or select an enabled model first.")
          : !selectedFile && !canRetryOriginal
            ? t("submissionUploadFileRequired")
            : !canRetryOriginal && identityMode === "roster" && !rosterFile
              ? t("submissionUploadRosterRequired")
              : null;

  function selectSubmission(file: File | undefined) {
    if (!file || isPending) return;
    if (!hasSuffix(file.name, SUBMISSION_SUFFIXES)) {
      setFormError(t("submissionUploadUnsupported"));
      return;
    }
    setSelectedFile(file);
    setUploadPercent(0);
    setFormError(null);
    setNeedsModel(false);
  }

  function selectRoster(file: File | undefined) {
    if (!file || isPending) return;
    if (!hasSuffix(file.name, ROSTER_SUFFIXES)) {
      setFormError(t("submissionUploadErrorRoster"));
      return;
    }
    setRosterFile(file);
    setFormError(null);
    setNeedsModel(false);
  }

  function handleSubmissionInput(event: ChangeEvent<HTMLInputElement>) {
    selectSubmission(event.target.files?.[0]);
    event.target.value = "";
  }

  function handleRosterInput(event: ChangeEvent<HTMLInputElement>) {
    selectRoster(event.target.files?.[0]);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDragging(false);
    selectSubmission(event.dataTransfer.files?.[0]);
  }

  async function handleStart() {
    setFormError(null);
    setNeedsModel(false);
    if (!taskId) {
      setFormError(t("submissionUploadTaskUnavailable"));
      return;
    }
    if (isRecognitionRunning && !selectedFile) {
      navigate(`/tasks/${taskId}/submissions/progress`);
      return;
    }
    if (uploadDisabledReason) {
      setFormError(uploadDisabledReason);
      return;
    }
    const replaceConfirmed = needsReplacementConfirmation && !canRetryOriginal
      ? window.confirm(t("submissionUploadReplaceConfirm"))
      : false;
    if (needsReplacementConfirmation && !canRetryOriginal && !replaceConfirmed) return;

    try {
      const response = canRetryOriginal && task?.last_failed_job_id
        ? await retryRecognition.mutateAsync({
            taskId,
            jobId: task.last_failed_job_id,
            recognitionProviderId,
            expectedWorkflowRevision: task.workflow_revision,
          })
        : await parseSubmissions.mutateAsync({
            taskId,
            file: selectedFile as File,
            identityMode,
            rosterFile: identityMode === "roster" ? rosterFile : null,
            recognitionProviderId,
            replaceConfirmed,
            onProgress: setUploadPercent,
          });
      if (response.status === "already_done") {
        submissionDrafts.delete(taskId);
        toast.info(t("submissionUploadViewProgress"));
        navigate(`/tasks/${taskId}/submissions`);
      } else {
        toast.success(t("submissionUploadStarted"));
        navigate(`/tasks/${taskId}/submissions/progress`);
      }
    } catch (error) {
      setNeedsModel([
        "no_provider_configured",
        "recognition_provider_not_enabled",
      ].includes(submissionErrorCode(error)));
      setFormError(localizeSubmissionError(error, t, locale));
    }
  }

  const identityHelp = identityMode === "roster"
    ? t("submissionUploadRosterHelp")
    : identityMode === "manual_review"
      ? t("submissionUploadManualHelp")
      : t("submissionUploadFilenameHelp");

  if (taskQuery.isSuccess && taskId && task && !hasTaskReachedStep(task, 3)) {
    return <Navigate replace to={getTaskDestination(task)} />;
  }

  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">{t("submissionUploadTitle")}</h1>

      <NewTaskStepper currentStep={3} />

      <div className="mx-auto mt-[45px] w-full max-w-[900px]">
        <div
          className={cn(
            "flex h-[230px] cursor-pointer flex-col items-center justify-center rounded-[12px] border bg-card px-6 text-center outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            isDragging ? "border-primary bg-primary/[0.03]" : "border-primary",
            isPending && "cursor-wait opacity-70",
          )}
          role="button"
          tabIndex={0}
          aria-label={t("submissionUploadChoose")}
          onClick={() => submissionInputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              submissionInputRef.current?.click();
            }
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setIsDragging(false)}
          onDrop={handleDrop}
        >
          <input
            ref={submissionInputRef}
            type="file"
            className="hidden"
            accept={SUBMISSION_SUFFIXES.join(",")}
            disabled={isPending}
            onChange={handleSubmissionInput}
          />
          <span className="inline-flex h-14 w-14 items-center justify-center rounded-full bg-primary/[0.14] text-primary">
            <FileUp aria-hidden="true" className="h-6 w-6" />
          </span>
          <p className="mt-[14px] text-[18px] font-semibold leading-[22px] text-foreground">
            {visibleFileName ?? t("submissionUploadDropTitle")}
          </p>
          <p className="mt-2 text-[13px] leading-5 text-muted-foreground">
            {selectedFile
              ? `${formatFileSize(selectedFile.size)} · ${t("submissionUploadOcrLimit")}`
              : canRetryOriginal
                ? localText(locale, "原文件已安全保留，可直接改选模型后重试。", "The original file is preserved; switch models and retry without uploading again.")
              : t("submissionUploadFormats")}
          </p>
          <span className="mt-[17px] inline-flex h-10 min-w-[130px] items-center justify-center rounded-[8px] border bg-card px-4 text-[14px] font-semibold text-foreground">
            {visibleFileName ? t("submissionUploadReplace") : t("submissionUploadChoose")}
          </span>
          {isPending ? (
            <div className="mt-3 h-1 w-[min(300px,70%)] overflow-hidden rounded-full bg-muted" aria-label={`${uploadPercent}%`}>
              <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${uploadPercent}%` }} />
            </div>
          ) : null}
        </div>

        <p className="mt-3 rounded-[8px] border border-blue-200 bg-blue-50/60 px-4 py-3 text-[12px] leading-5 text-blue-900 dark:border-blue-900 dark:bg-blue-950/20 dark:text-blue-100">
          {t("submissionUploadFileContract")}
        </p>

        <StageProviderSelect
          id="submission-recognition-provider"
          label={localText(locale, "作答识别模型", "Submission recognition model")}
          hint={localText(
            locale,
            "已自动选择默认模型；有多个模型时可在这里改选。图片或扫描版 PDF 需要支持图片/视觉输入的模型，若失败会明确显示认证、模型、限流、网络或视觉能力原因。",
            "Your default model is selected automatically; choose another here when needed. Images and scanned PDFs require visual input support; failures identify authentication, model, rate-limit, network, or vision capability errors.",
          )}
          experts={enabledExperts}
          value={recognitionProviderId}
          disabled={isPending || expertsQuery.isLoading}
          locale={locale}
          onChange={(providerId) => {
            setRecognitionProviderId(providerId);
            setFormError(null);
            setNeedsModel(false);
          }}
          className="mt-5"
        />

        <section className="mt-10 flex min-h-[145px] flex-col rounded-[10px] border bg-card px-[29px] pb-5 pt-[27px] sm:h-[145px]">
          <h2 className="text-[18px] font-bold leading-[22px] text-foreground">
            {t("submissionUploadIdentityTitle")}
          </h2>
          <div className="mt-[18px] flex flex-wrap gap-3 sm:gap-5" role="radiogroup" aria-label={t("submissionUploadIdentityTitle")}>
            {IDENTITY_OPTIONS.map((option) => (
              <button
                key={option.mode}
                type="button"
                role="radio"
                aria-checked={identityMode === option.mode}
                disabled={isPending}
                onClick={() => {
                  setIdentityMode(option.mode);
                  setFormError(null);
                  setNeedsModel(false);
                }}
                className={cn(
                  "inline-flex h-7 items-center justify-center rounded-full px-3 text-[12px] font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                  option.mode === "filename" ? "min-w-[150px]" : "min-w-[100px]",
                  identityMode === option.mode
                    ? "bg-primary/[0.14] text-primary"
                    : "bg-muted/60 text-muted-foreground hover:text-foreground",
                )}
              >
                {t(option.label)}
              </button>
            ))}
          </div>
          <div className="mt-auto flex min-w-0 flex-col gap-1.5 pt-3 text-[13px] leading-5 text-muted-foreground sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:pt-2">
            <p className="min-w-0 truncate" title={identityHelp}>
              {identityMode === "filename" && selectedFile
                ? `${t("submissionUploadPreviewPrefix")}${isSubmissionArchive(selectedFile.name)
                  ? t("submissionUploadArchivePreview")
                  : filenameIdentityPreview(selectedFile.name)}`
                : identityHelp}
            </p>
            {identityMode === "roster" ? (
              <div className="flex shrink-0 items-center gap-2">
                <span className="max-w-[180px] truncate text-[12px]" title={rosterFile?.name}>
                  {rosterFile?.name ?? t("submissionUploadRosterFormats")}
                </span>
                <input
                  ref={rosterInputRef}
                  type="file"
                  className="hidden"
                  accept={ROSTER_SUFFIXES.join(",")}
                  disabled={isPending}
                  onChange={handleRosterInput}
                />
                <button
                  type="button"
                  className="inline-flex h-7 items-center rounded-[6px] border bg-card px-2.5 text-[12px] font-semibold text-foreground hover:bg-muted"
                  onClick={() => rosterInputRef.current?.click()}
                >
                  {rosterFile ? t("submissionUploadRosterReplace") : t("submissionUploadRosterChoose")}
                </button>
              </div>
            ) : null}
          </div>
        </section>

        <div className="mt-[31px] flex min-h-10 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0 text-[13px] leading-5">
            {formError ? (
              <p id="submission-upload-action-message" role="alert" className="text-danger">
                {formError}
                {needsModel && taskId ? (
                  <Link
                    to={`/settings/byok?returnTo=${encodeURIComponent(`/tasks/${taskId}/submissions/upload`)}`}
                    className="ml-2 font-semibold text-primary underline underline-offset-2"
                  >
                    {t("submissionUploadConfigureModels")}
                  </Link>
                ) : null}
              </p>
            ) : hasExistingSubmissions ? (
              <p id="submission-upload-action-message" className="text-muted-foreground">
                {t("submissionUploadExisting")} {" "}
                <Link to="/tasks/new" className="font-semibold text-primary underline-offset-2 hover:underline">
                  {t("submissionUploadCreateSeparateTask")}
                </Link>
              </p>
            ) : uploadDisabledReason && uploadDisabledReason !== t("submissionUploadFileRequired") ? (
              <p id="submission-upload-action-message" className="text-muted-foreground">
                {uploadDisabledReason}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className="inline-flex h-10 w-full shrink-0 items-center justify-center rounded-[8px] bg-primary px-4 text-[14px] font-semibold leading-[18px] text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 sm:w-[180px]"
            disabled={isPending || isWorkflowBusy}
            title={uploadDisabledReason ?? undefined}
            aria-describedby={formError || hasExistingSubmissions || uploadDisabledReason ? "submission-upload-action-message" : undefined}
            onClick={() => void handleStart()}
          >
            {isPending ? (
              <><LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" />{t("submissionUploadStarting")}</>
            ) : isRecognitionRunning && !selectedFile
              ? t("submissionUploadViewProgress")
              : canRetryOriginal
                ? localText(locale, "用所选模型重试", "Retry with selected model")
              : hasExistingSubmissions
                ? t("submissionUploadOverwriteStart")
                : t("submissionUploadStart")}
          </button>
        </div>
      </div>
    </div>
  );
}

function hasSuffix(filename: string, suffixes: readonly string[]) {
  const normalized = filename.toLowerCase();
  return suffixes.some((suffix) => normalized.endsWith(suffix));
}

function filenameIdentityPreview(filename: string) {
  const normalized = filename.replace(/\.(?:tar\.gz|tar\.bz2|[^.]+)$/i, "");
  if (/\.(?:zip|rar|7z|tar|tgz|tbz2)$/i.test(filename)) {
    return normalized;
  }
  const tokens = normalized.split(/[_\-\s]+/).map((token) => token.trim()).filter(Boolean);
  const studentId = tokens.find((token) => /\d{4,}/.test(token));
  const studentName = tokens.find((token) => token !== studentId && /[A-Za-z\u3400-\u9fff]{2,}/.test(token));
  return studentId
    ? `${filename} → ${studentId}${studentName ? ` / ${studentName}` : ""}`
    : `${filename} → ${normalized}`;
}

function isSubmissionArchive(filename: string) {
  return /\.(?:zip|rar|7z|tar|tar\.gz|tgz|tar\.bz2|tbz2)$/i.test(filename);
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function localizeSubmissionError(
  error: unknown,
  t: (key: MessageKey) => string,
  locale: "zh-CN" | "en-US",
) {
  const normalized = normalizeAPIError(error);
  const code = submissionErrorCode(error);
  if (["submission_source_unsupported", "submission_source_empty", "submission_archive_empty", "submission_archive_invalid"].includes(code)) {
    return t("submissionUploadErrorInvalid");
  }
  if (["submission_source_too_large", "pdf_page_limit_exceeded", "pdf_character_limit_exceeded"].includes(code)) {
    return t("submissionUploadErrorTooLarge");
  }
  if (code.startsWith("submission_roster_")) return t("submissionUploadErrorRoster");
  if (["no_provider_configured", "recognition_provider_not_enabled"].includes(code)) {
    return t("submissionUploadErrorProvider");
  }
  if (code === "provider_vision_not_supported") {
    return localText(locale, "所选模型不支持这份图片或扫描版 PDF 的视觉输入。原文件已保留，请改选支持视觉的模型后重试。", "The selected model does not support visual input for this image or scanned PDF. The original file is preserved; choose a vision-capable model and retry.");
  }
  if (code === "vision_provider_required") {
    return localText(locale, "这份文件需要视觉识别，请选择一个支持图片输入的模型。", "This file requires visual recognition. Choose a model that supports image input.");
  }
  if (code === "provider_auth_failed") {
    return localText(locale, "所选模型的 API Key 无效或没有调用权限。请在模型与 BYOK 中修正后重试。", "The selected model's API key is invalid or lacks permission. Fix it in Models & BYOK and retry.");
  }
  if (code === "provider_model_not_found") {
    return localText(locale, "所选模型名称不存在或当前账户无权使用。请检查模型名称或改选其他模型。", "The selected model was not found or is unavailable to this account. Check the model name or choose another model.");
  }
  if (code === "provider_rate_limited") {
    return localText(locale, "服务商限制了本次请求，原文件已保留，请稍后重试。", "The provider rate-limited this request. The original file is preserved; try again later.");
  }
  if (code === "provider_timeout") {
    return localText(locale, "所选模型响应超时，原文件已保留，请重试或改选其他模型。", "The selected model timed out. The original file is preserved; retry or choose another model.");
  }
  if (code === "provider_unreachable") {
    return localText(locale, "暂时无法连接所选模型服务，原文件已保留。请检查网络或中转地址后重试。", "The selected model service is unreachable. The original file is preserved; check the network or relay URL and retry.");
  }
  if (code === "provider_request_rejected") {
    return localText(locale, "所选服务商拒绝了请求。请检查模型与接口配置后重试。", "The selected provider rejected the request. Check the model and endpoint configuration, then retry.");
  }
  if (["submission_retry_not_available", "submission_retry_source_unavailable"].includes(code)) {
    return localText(locale, "原文件已不可用于直接重试，请重新选择文件。", "The original file is no longer available for direct retry. Select the file again.");
  }
  if (["workflow_busy", "different_submission_running"].includes(code)) return t("submissionUploadBusy");
  if (["invalid_state", "stale_revision", "replacement_confirmation_required"].includes(code)) {
    return t("submissionUploadErrorConflict");
  }
  return normalized.status === 0 && normalized.message
    ? normalized.message
    : t("submissionUploadErrorGeneric");
}

function submissionErrorCode(error: unknown) {
  return getAPIErrorCode(error) ?? "";
}

function localText(locale: "zh-CN" | "en-US", zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

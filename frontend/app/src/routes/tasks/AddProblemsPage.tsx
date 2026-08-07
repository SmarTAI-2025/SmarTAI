import {
  AlignLeft,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  FileText,
  KeyRound,
  LoaderCircle,
  Plus,
  Search,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent, type DragEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import {
  useExperts,
  useProblemSourceLibrary,
  useProblemSourcePreflight,
  useQuestionPreparationCapabilities,
  useStartQuestionPreparation,
  useTask,
} from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { RecoverableActionState, type RecoveryAction } from "@/components/ui/RecoverableActionState";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import type {
  PreparationSourceRole,
  ProblemLibraryMaterial,
  ProblemSourceMode,
  ProblemSourceScope,
  ProblemStructureMode,
  QuestionScorePolicyInput,
} from "@/types";

const SOURCE_ROLES: PreparationSourceRole[] = ["problem", "reference_answer", "rubric", "programming_tests"];
const DOCUMENT_SOURCE_EXTENSIONS = [".pdf", ".txt", ".md", ".markdown"] as const;
const IMAGE_SOURCE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".webp"]);

type SourceDraft = {
  id: string;
  role: PreparationSourceRole;
  sourceMode: ProblemSourceMode;
  file: File | null;
  libraryScope: ProblemSourceScope;
  librarySearch: string;
  libraryMaterial: ProblemLibraryMaterial | null;
  inlineText: string;
  structureMode: ProblemStructureMode;
  extractionHint: string;
  saveToLibrary: boolean;
};

type ScorePolicyDraft = {
  mode: QuestionScorePolicyInput["mode"];
  uniformMaxScore: string;
  perQuestionText: string;
};

const DEFAULT_SCORE_POLICY_DRAFT: ScorePolicyDraft = {
  mode: "default_10",
  uniformMaxScore: "10",
  perQuestionText: "",
};

type AddProblemsRouteState = {
  questionPreparationDraft?: {
    taskId: string;
    activeRole: PreparationSourceRole;
    sources: SourceDraft[];
    scorePolicy?: ScorePolicyDraft;
  };
};

type PreparationFailure = {
  error: unknown;
  phase: "source_preflight" | "question_preparation";
  sourceId?: string;
  sourceRole?: PreparationSourceRole;
};

export function AddProblemsPage() {
  const { taskId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const restored = getRestoredDraft(location.state, taskId);
  const taskQuery = useTask(taskId);
  const capabilitiesQuery = useQuestionPreparationCapabilities(taskId);
  const expertsQuery = useExperts();
  const preflight = useProblemSourcePreflight();
  const startPreparation = useStartQuestionPreparation();
  const [activeRole, setActiveRole] = useState<PreparationSourceRole>(restored?.activeRole ?? "problem");
  const [sources, setSources] = useState<SourceDraft[]>(restored?.sources ?? [createSourceDraft("problem")]);
  const [scorePolicy, setScorePolicy] = useState<ScorePolicyDraft>(() => (
    restored?.scorePolicy ?? DEFAULT_SCORE_POLICY_DRAFT
  ));
  const [formError, setFormError] = useState<string | null>(null);
  const [preparationFailure, setPreparationFailure] = useState<PreparationFailure | null>(null);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  const [showStartRequirements, setShowStartRequirements] = useState(false);

  const activeSources = sources.filter((source) => source.role === activeRole);
  const configuredSources = sources.filter(sourceHasValue);
  const enabledExperts = (expertsQuery.data ?? []).filter((expert) => expert.enabled);
  const isBusy = preflight.isPending || startPreparation.isPending;
  const hasExistingProblems = Boolean(taskQuery.data?.problem_file_name || taskQuery.data?.problem_count);
  const hasRecognizedProblems = (taskQuery.data?.problem_count ?? 0) > 0;
  const hasProblemSource = configuredSources.some((source) => source.role === "problem");
  const taskReturnPath = taskId ? `/tasks/${taskId}/upload/problems` : "/tasks/new";
  const routeState: AddProblemsRouteState = {
    questionPreparationDraft: {
      taskId: taskId ?? "",
      activeRole,
      sources,
      scorePolicy,
    },
  };

  const needsByok = enabledExperts.length === 0 && !expertsQuery.isLoading && !expertsQuery.isError;
  const needsProblemSource = !hasProblemSource;
  const startBlocked = needsByok || needsProblemSource;
  const primaryDisabledReason = expertsQuery.isLoading
    ? tx(locale, "正在读取模型配置。", "Loading model configuration.")
    : expertsQuery.isError
      ? tx(locale, "模型配置暂时不可用。", "Model configuration is unavailable.")
      : needsByok && needsProblemSource
        ? tx(locale, "还需要上传题目并启用一个 BYOK 模型。", "Add questions and enable a BYOK model first.")
        : needsByok
        ? tx(locale, "需要先添加并启用一个 BYOK 模型。", "Add and enable a BYOK model first.")
        : needsProblemSource
          ? tx(locale, "至少添加一份题目来源。", "Add at least one question source.")
          : null;

  function updateSource(id: string, patch: Partial<SourceDraft>) {
    setSources((current) => current.map((source) => source.id === id ? { ...source, ...patch } : source));
    setFormError(null);
    setPreparationFailure(null);
  }

  function addSource(role: PreparationSourceRole) {
    setSources((current) => [...current, createSourceDraft(role)]);
    setFormError(null);
    setPreparationFailure(null);
  }

  function removeSource(id: string) {
    setSources((current) => current.filter((source) => source.id !== id));
    setFormError(null);
    setPreparationFailure(null);
  }

  function moveRole(direction: -1 | 1) {
    const current = SOURCE_ROLES.indexOf(activeRole);
    const next = Math.max(0, Math.min(SOURCE_ROLES.length - 1, current + direction));
    setActiveRole(SOURCE_ROLES[next]);
  }

  async function handleStart() {
    setFormError(null);
    setPreparationFailure(null);
    if (!taskId || !taskQuery.data) {
      setFormError(tx(locale, "任务信息不可用，请刷新后重试。", "Task details are unavailable. Refresh and retry."));
      return;
    }
    if (primaryDisabledReason) {
      if (startBlocked) setShowStartRequirements(true);
      else setFormError(primaryDisabledReason);
      return;
    }
    const resolvedScorePolicy = validateScorePolicy(
      scorePolicy,
      locale,
      capabilitiesQuery.data?.score_policy?.maximum_max_score ?? 10_000,
    );
    if (!resolvedScorePolicy.value) {
      setActiveRole("rubric");
      setFormError(resolvedScorePolicy.error);
      return;
    }
    let activeSource: SourceDraft | undefined;
    let phase: PreparationFailure["phase"] = "source_preflight";
    try {
      const tokens: string[] = [];
      for (let index = 0; index < configuredSources.length; index += 1) {
        const source = configuredSources[index];
        activeSource = source;
        setBusyLabel(tx(locale, `正在检查资料 ${index + 1}/${configuredSources.length}`, `Checking source ${index + 1}/${configuredSources.length}`));
        const result = await preflight.mutateAsync({
          taskId,
          role: source.role,
          mode: source.sourceMode,
          file: source.file,
          libraryMaterialId: source.libraryMaterial?.material_id,
          inlineText: source.inlineText,
          structureMode: source.structureMode,
          extractionHint: source.extractionHint,
          saveToLibrary: source.sourceMode === "upload" && source.saveToLibrary,
        });
        tokens.push(result.source_token);
      }
      phase = "question_preparation";
      activeSource = undefined;
      setBusyLabel(tx(locale, "正在启动统一识别", "Starting unified preparation"));
      const response = await startPreparation.mutateAsync({
        taskId,
        sourceTokens: tokens,
        expectedWorkflowRevision: taskQuery.data.workflow_revision,
        replaceConfirmed: hasExistingProblems,
        scorePolicy: resolvedScorePolicy.value,
      });
      toast.success(
        ["already_running", "already_done"].includes(response.status)
          ? tx(locale, "已有相同的题目准备任务。", "The same preparation job already exists.")
          : tx(locale, "题目与资料已进入统一识别。", "Question materials are being prepared."),
      );
      navigate(`/tasks/${taskId}/problems/progress`);
    } catch (error) {
      setPreparationFailure({
        error,
        phase,
        sourceId: activeSource?.id,
        sourceRole: activeSource?.role,
      });
    } finally {
      setBusyLabel(null);
    }
  }

  const roleIndex = SOURCE_ROLES.indexOf(activeRole);
  const recoveryInfo = preparationFailure
    ? classifyRecoverableError(preparationFailure.error, {
      locale,
      phase: preparationFailure.phase,
      returnTo: taskReturnPath,
    })
    : null;
  const recoveryPrimary = recoveryInfo && preparationFailure
    ? getPreparationRecoveryAction({
      info: recoveryInfo,
      failure: preparationFailure,
      routeState,
      locale,
      onRetry: () => void handleStart(),
      onRefresh: () => void Promise.all([taskQuery.refetch(), expertsQuery.refetch()]),
      onOpenSource: (role, sourceId, clearLibrary) => {
        setActiveRole(role);
        if (clearLibrary && sourceId) updateSource(sourceId, { libraryMaterial: null });
        window.requestAnimationFrame(() => {
          if (sourceId) document.getElementById(`problem-source-file-${sourceId}`)?.click();
        });
      },
    })
    : undefined;
  return (
    <div className="w-full max-w-[1300px]">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {tx(locale, "题目与资料上传", "Add Questions & Supporting Materials")}
      </h1>
      <NewTaskStepper currentStep={1} reachableStep={hasRecognizedProblems ? 2 : 1} returnState={routeState} />

      <div className="mx-auto mt-6 w-full max-w-[940px]">
        <section className="overflow-hidden rounded-[10px] border bg-card">
          <div className="flex items-center justify-between border-b px-5 py-3 sm:px-7">
            <button
              type="button"
              onClick={() => moveRole(-1)}
              disabled={roleIndex === 0 || isBusy}
              className="inline-flex h-9 w-9 items-center justify-center rounded-[7px] border text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-30"
              aria-label={tx(locale, "上一类资料", "Previous source type")}
            >
              <ArrowLeft aria-hidden="true" className="h-4 w-4" />
            </button>
            <div className="min-w-0 text-center">
              <p className="text-[17px] font-bold text-foreground">{roleMeta(activeRole, locale).title}</p>
              <p className="mt-1 text-xs text-muted-foreground">{roleMeta(activeRole, locale).description}</p>
            </div>
            <button
              type="button"
              onClick={() => moveRole(1)}
              disabled={roleIndex === SOURCE_ROLES.length - 1 || isBusy}
              className="inline-flex h-9 w-9 items-center justify-center rounded-[7px] border text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-30"
              aria-label={tx(locale, "下一类资料", "Next source type")}
            >
              <ArrowRight aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>

          <nav className="grid grid-cols-2 border-b sm:grid-cols-4" aria-label={tx(locale, "题目资料类型", "Question material types")}>
            {SOURCE_ROLES.map((role, index) => {
              const count = sources.filter((source) => source.role === role && sourceHasValue(source)).length;
              const active = role === activeRole;
              return (
                <button
                  key={role}
                  type="button"
                  onClick={() => setActiveRole(role)}
                  className={cn(
                    "min-h-[54px] border-b-2 px-3 text-left transition-colors sm:text-center",
                    active ? "border-primary bg-blue-50/60 text-primary dark:bg-blue-950/20" : "border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                  )}
                >
                  <span className="block text-[13px] font-semibold">{index + 1}. {roleMeta(role, locale).shortTitle}</span>
                  <span className="mt-1 block text-[11px]">{role === "problem" ? tx(locale, "必填", "Required") : tx(locale, "可选", "Optional")} · {count}</span>
                </button>
              );
            })}
          </nav>

          <div className="space-y-3 bg-slate-50/60 p-3 dark:bg-slate-950/20 sm:p-4">
            {activeRole === "rubric" ? (
              <ScorePolicyEditor
                value={scorePolicy}
                disabled={isBusy}
                maximumMaxScore={capabilitiesQuery.data?.score_policy?.maximum_max_score ?? 10_000}
                maxTextLength={capabilitiesQuery.data?.score_policy?.per_question_text_max_characters ?? 12_000}
                locale={locale}
                onChange={(patch) => {
                  setScorePolicy((current) => ({ ...current, ...patch }));
                  setFormError(null);
                  setPreparationFailure(null);
                }}
              />
            ) : null}
            {activeSources.length ? activeSources.map((source, index) => (
              <SourceEditor
                key={source.id}
                source={source}
                number={index + 1}
                taskId={taskId}
                taskReady={taskQuery.isSuccess}
                disabled={isBusy}
                hasTaskCourse={Boolean(taskQuery.data?.course_id)}
                acceptedExtensions={capabilitiesQuery.data?.source_roles[source.role]?.accepted_extensions}
                locale={locale}
                onUpdate={(patch) => updateSource(source.id, patch)}
                onRemove={() => removeSource(source.id)}
              />
            )) : (
              <div className="rounded-[10px] border border-dashed bg-card px-5 py-12 text-center">
                <p className="text-sm font-semibold text-foreground">{tx(locale, "尚未添加这类资料", "No source added for this type")}</p>
                <p className="mt-1 text-xs text-muted-foreground">{tx(locale, "可以跳过，系统会在统一识别时生成需要的内容。", "You can skip it; required content will be generated during preparation.")}</p>
              </div>
            )}
            <button
              type="button"
              disabled={isBusy}
              onClick={() => addSource(activeRole)}
              className="inline-flex h-10 items-center gap-2 rounded-[8px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted disabled:opacity-50"
            >
              <Plus aria-hidden="true" className="h-4 w-4" />
              {roleMeta(activeRole, locale).addLabel}
            </button>
          </div>
        </section>

        <section className="mt-4 flex flex-col gap-3 rounded-[10px] border bg-card px-5 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-7">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground">{tx(locale, "一次识别并准备全部题目资料", "Prepare all question materials in one step")}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {enabledExperts.length
                ? tx(
                  locale,
                  `已启用 ${enabledExperts.length} 个模型；未上传的标答和评分标准会由 AI 生成。`,
                  enabledExperts.length === 1
                    ? "1 model is enabled. Missing reference answers and rubrics will be generated."
                    : `${enabledExperts.length} models are enabled. Missing reference answers and rubrics will be generated.`,
                )
                : tx(locale, "尚未启用模型；点击主按钮可查看原因并前往 BYOK。", "No model is enabled. Use the main button to open BYOK guidance.")}
            </p>
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
              {capabilitiesQuery.data?.reader.ocr
                ? tx(locale, "文件能力由当前识别服务动态提供。", "Available file readers are provided by the active recognition service.")
                : tx(locale, "PDF、TXT 与 Markdown 可直接读取；图片和扫描版 PDF 的 OCR 需要已启用的视觉模型，DOCX 暂不支持。", "PDF, TXT, and Markdown are read directly. Image and scanned-PDF OCR requires an enabled vision model; DOCX is not supported yet.")}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void handleStart()}
            disabled={isBusy || expertsQuery.isLoading || expertsQuery.isError}
            title={primaryDisabledReason ?? undefined}
            className={cn(
              "inline-flex h-10 w-full shrink-0 items-center justify-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45 sm:w-[210px]",
              startBlocked && "cursor-help opacity-50 hover:opacity-65",
            )}
          >
            {isBusy ? <LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" /> : null}
            {busyLabel ?? (hasExistingProblems
              ? tx(locale, "重新识别全部资料", "Prepare All Materials Again")
              : tx(locale, "识别并准备题目资料", "Prepare Question Materials"))}
          </button>
        </section>
        {formError ? <p role="alert" className="mt-3 text-sm text-danger">{formError}</p> : null}
        {recoveryInfo ? (
          <RecoverableActionState
            info={recoveryInfo}
            locale={locale}
            compact
            className="mt-4"
            primaryAction={recoveryPrimary}
            secondaryAction={{
              label: tx(locale, "关闭提示", "Dismiss"),
              onClick: () => setPreparationFailure(null),
            }}
          />
        ) : null}
      </div>

      <StartRequirementsDialog
        open={showStartRequirements}
        needsByok={needsByok}
        needsProblemSource={needsProblemSource}
        returnTo={taskReturnPath}
        routeState={routeState}
        locale={locale}
        onClose={() => setShowStartRequirements(false)}
        onGoToProblems={() => {
          setActiveRole("problem");
          setShowStartRequirements(false);
        }}
      />
    </div>
  );
}

function ScorePolicyEditor({ value, disabled, maximumMaxScore, maxTextLength, locale, onChange }: {
  value: ScorePolicyDraft;
  disabled: boolean;
  maximumMaxScore: number;
  maxTextLength: number;
  locale: string;
  onChange: (patch: Partial<ScorePolicyDraft>) => void;
}) {
  const differentPerQuestion = value.mode === "per_question";
  return (
    <section className="rounded-[10px] border border-blue-200 bg-card p-4 shadow-sm dark:border-blue-900 sm:p-5" aria-labelledby="score-policy-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="score-policy-title" className="text-sm font-bold text-foreground">{tx(locale, "设置题目满分", "Set Question Maximum Scores")}</h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {tx(locale, "评分步骤统一使用百分比；这里的满分会成为批改时不可被模型覆盖的分值尺度。", "Rubric steps use percentages. These maximum scores become the grading scale the model cannot override.")}
          </p>
        </div>
        <label className="inline-flex shrink-0 cursor-pointer items-center gap-2 text-xs font-semibold text-foreground">
          <input
            type="checkbox"
            checked={differentPerQuestion}
            disabled={disabled}
            onChange={(event) => onChange({
              mode: event.target.checked
                ? "per_question"
                : value.uniformMaxScore === "10" ? "default_10" : "uniform",
            })}
            className="h-4 w-4 rounded border-border accent-primary"
          />
          {tx(locale, "每题满分不同", "Different score per question")}
        </label>
      </div>

      {differentPerQuestion ? (
        <label className="mt-4 block">
          <span className="text-xs font-semibold text-foreground">{tx(locale, "按题号描述每题满分", "Describe maximum scores by question number")}</span>
          <textarea
            aria-label={tx(locale, "每题满分说明", "Per-question maximum score instructions")}
            value={value.perQuestionText}
            disabled={disabled}
            maxLength={maxTextLength}
            onChange={(event) => onChange({ perQuestionText: event.target.value })}
            placeholder={tx(locale, "例如：第 1–3 题每题 5 分，第 4 题 15 分，第 5 题 20 分。", "Example: Questions 1–3 are worth 5 points each; Question 4 is worth 15; Question 5 is worth 20.")}
            className="mt-2 min-h-[88px] w-full resize-y rounded-[7px] border bg-background px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
          />
          <span className="mt-1.5 block text-[11px] leading-4 text-muted-foreground">{tx(locale, "识别后会逐题显示结果；未匹配到的题目按 10 分并明确提醒复核。", "After recognition, every result is shown. Unmatched questions default to 10 and are explicitly flagged for review.")}</span>
        </label>
      ) : (
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
          <label className="block w-full sm:max-w-[220px]">
            <span className="text-xs font-semibold text-foreground">{tx(locale, "每题满分", "Maximum score per question")}</span>
            <div className="relative mt-2">
              <input
                aria-label={tx(locale, "每题满分", "Maximum score per question")}
                type="number"
                inputMode="decimal"
                min="0.01"
                max={maximumMaxScore}
                step="0.5"
                value={value.uniformMaxScore}
                disabled={disabled}
                onChange={(event) => onChange({ mode: "uniform", uniformMaxScore: event.target.value })}
                className="h-10 w-full rounded-[7px] border bg-background px-3 pr-10 text-sm font-semibold text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
              <span className="pointer-events-none absolute right-3 top-2.5 text-xs text-muted-foreground">{tx(locale, "分", "pts")}</span>
            </div>
          </label>
          <div className="flex min-w-0 flex-1 items-center justify-between gap-3 rounded-[7px] bg-slate-50 px-3 py-2 dark:bg-slate-950/20">
            <p className="text-[11px] leading-4 text-muted-foreground">
              {value.mode === "default_10"
                ? tx(locale, "当前未主动设置：系统先按每题 10 分，题目审核时会提醒确认。", "Not explicitly set: questions default to 10 and the review step will ask for confirmation.")
                : tx(locale, "当前数值会应用到全部识别出的题目。", "This value will be applied to every recognized question.")}
            </p>
            {value.mode !== "default_10" ? (
              <button type="button" disabled={disabled} onClick={() => onChange({ mode: "default_10", uniformMaxScore: "10" })} className="shrink-0 text-[11px] font-semibold text-primary hover:underline">
                {tx(locale, "恢复默认", "Use default")}
              </button>
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}

function SourceEditor({
  source,
  number,
  taskId,
  taskReady,
  disabled,
  hasTaskCourse,
  acceptedExtensions,
  locale,
  onUpdate,
  onRemove,
}: {
  source: SourceDraft;
  number: number;
  taskId: string | undefined;
  taskReady: boolean;
  disabled: boolean;
  hasTaskCourse: boolean;
  acceptedExtensions?: string[];
  locale: string;
  onUpdate: (patch: Partial<SourceDraft>) => void;
  onRemove: () => void;
}) {
  const [dragging, setDragging] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const librarySearch = useImeSafeQuery({
    value: source.librarySearch,
    onCommit: (value) => onUpdate({ librarySearch: value }),
  });
  const debouncedLibrarySearch = useDebouncedValue(source.librarySearch, 220);
  const libraryQuery = useProblemSourceLibrary(
    taskId,
    source.libraryScope,
    debouncedLibrarySearch,
    taskReady && source.sourceMode === "library",
  );
  const libraryItems: ProblemLibraryMaterial[] = libraryQuery.data?.items ?? [];
  const libraryLoading = libraryQuery.isFetching;
  const accepted = acceptedExtensions?.length
    ? acceptedExtensions.map((extension) => extension.toLowerCase())
    : source.role === "programming_tests"
      ? [...DOCUMENT_SOURCE_EXTENSIONS, ".json"]
      : [...DOCUMENT_SOURCE_EXTENSIONS];
  const accept = accepted.join(",");

  function selectFile(file?: File) {
    if (!file || disabled) return;
    const validationError = sourceFileValidationError(
      file,
      source.role,
      accepted,
      locale,
    );
    if (validationError) {
      setFileError(validationError);
      return;
    }
    setFileError(null);
    onUpdate({ file, sourceMode: "upload", libraryMaterial: null });
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    selectFile(event.dataTransfer.files?.[0]);
  }

  return (
    <article className="overflow-hidden rounded-[10px] border bg-card">
      <header className="flex items-center justify-between border-b px-4 py-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-2">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-blue-50 text-xs font-bold text-primary dark:bg-blue-950/50">{number}</span>
          <p className="truncate text-sm font-semibold text-foreground">{sourceSummary(source, locale)}</p>
        </div>
        <button type="button" disabled={disabled} onClick={onRemove} className="inline-flex h-8 w-8 items-center justify-center rounded-[6px] text-muted-foreground hover:bg-red-50 hover:text-danger disabled:opacity-40" aria-label={tx(locale, "删除这份资料", "Remove this source")}>
          <Trash2 aria-hidden="true" className="h-4 w-4" />
        </button>
      </header>

      <div className="p-3 sm:p-4">
        <div className={cn("grid gap-2 rounded-[8px] bg-muted/60 p-1", source.role === "rubric" ? "grid-cols-3" : "grid-cols-2")}>
          <ModeButton active={source.sourceMode === "upload"} disabled={disabled} onClick={() => onUpdate({ sourceMode: "upload", libraryMaterial: null })} icon={<UploadCloud className="h-4 w-4" />} label={tx(locale, "上传新文件", "Upload File")} />
          {source.role === "rubric" ? (
            <ModeButton active={source.sourceMode === "inline_text"} disabled={disabled} onClick={() => onUpdate({ sourceMode: "inline_text", file: null, libraryMaterial: null })} icon={<AlignLeft className="h-4 w-4" />} label={tx(locale, "自然语言描述", "Describe")} />
          ) : null}
          <ModeButton active={source.sourceMode === "library"} disabled={disabled} onClick={() => onUpdate({ sourceMode: "library", file: null })} icon={<BookOpen className="h-4 w-4" />} label={tx(locale, "课程资料库", "Course Library")} />
        </div>

        {source.sourceMode === "upload" ? (
          <div
            aria-label={tx(locale, `${roleMeta(source.role, locale).sourceLabel}文件上传`, `${roleMeta(source.role, locale).sourceLabel} file upload`)}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }}
            onDrop={handleDrop}
            className={cn("mt-3 flex min-h-[112px] flex-col items-center justify-center rounded-[9px] border border-dashed px-5 text-center transition-colors", dragging ? "border-primary bg-blue-50/60 dark:bg-blue-950/20" : "bg-slate-50/70 dark:bg-slate-950/20")}
          >
            <FileText aria-hidden="true" className="h-6 w-6 text-primary" />
            <p className="mt-1.5 max-w-full truncate text-sm font-semibold text-foreground">{source.file?.name ?? tx(locale, "拖入或选择文件", "Drop or choose a file")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{accept.replaceAll(".", "").toUpperCase().replaceAll(",", " / ")}</p>
            <label className="mt-2 inline-flex h-9 cursor-pointer items-center rounded-[7px] border bg-card px-4 text-xs font-semibold text-foreground hover:bg-muted">
              {source.file ? tx(locale, "替换文件", "Replace File") : tx(locale, "选择文件", "Choose File")}
              <input id={`problem-source-file-${source.id}`} type="file" accept={accept} className="sr-only" disabled={disabled} onChange={(event: ChangeEvent<HTMLInputElement>) => { selectFile(event.target.files?.[0]); event.target.value = ""; }} />
            </label>
            {fileError ? <p role="alert" className="mt-2 text-xs font-medium text-danger">{fileError}</p> : null}
          </div>
        ) : source.sourceMode === "inline_text" ? (
          <label className="mt-3 block rounded-[9px] border bg-slate-50/70 p-3 dark:bg-slate-950/20">
            <span className="text-xs font-semibold text-foreground">{tx(locale, "用自然语言描述评分标准", "Describe the grading rubric")}</span>
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              {tx(locale, "SmarTAI 会把描述整理为与标答步骤对应、可审核的评分标准。", "SmarTAI will align your description with the reference-answer steps for review.")}
            </span>
            <textarea
              value={source.inlineText ?? ""}
              disabled={disabled}
              maxLength={12000}
              onChange={(event) => onUpdate({ inlineText: event.target.value })}
              placeholder={tx(locale, "例如：推导过程占 60%，最终结果占 40%；允许等价表达。也可以按题号分别描述。", "Example: Reasoning is 60% and the final answer is 40%. Equivalent expressions are accepted; rules may also be described by question number.")}
              className="mt-2 min-h-[92px] w-full resize-y rounded-[7px] border bg-card px-3 py-2 text-sm font-normal leading-5 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
            />
          </label>
        ) : (
          <div className="mt-3 rounded-[9px] border p-3">
            <div className="grid gap-3 sm:grid-cols-[150px_minmax(0,1fr)]">
              <select value={source.libraryScope} disabled={disabled} onChange={(event) => onUpdate({ libraryScope: event.target.value as ProblemSourceScope, libraryMaterial: null })} className="h-10 rounded-[7px] border bg-card px-3 text-sm outline-none focus:border-primary">
                <option value="course">{tx(locale, "当前课程", "Current Course")}</option>
                <option value="all">{tx(locale, "全部资料", "All Materials")}</option>
              </select>
              <label className="relative">
                <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
                <input value={librarySearch.draftValue} disabled={disabled} onBlur={librarySearch.handleBlur} onChange={librarySearch.handleChange} onCompositionEnd={librarySearch.handleCompositionEnd} onCompositionStart={librarySearch.handleCompositionStart} placeholder={tx(locale, "搜索资料名称", "Search materials")} className="h-10 w-full rounded-[7px] border bg-card pl-9 pr-3 text-sm outline-none focus:border-primary" />
              </label>
            </div>
            {!hasTaskCourse && source.libraryScope === "course" ? <p className="mt-3 text-xs text-warning">{tx(locale, "当前任务未选择课程，请改选全部资料。", "This task has no course; choose all materials.")}</p> : null}
            <div className="mt-3 max-h-44 overflow-y-auto rounded-[7px] border">
              {libraryLoading ? <p className="px-4 py-6 text-center text-xs text-muted-foreground">{tx(locale, "正在读取资料库…", "Loading library…")}</p> : libraryItems.length ? libraryItems.map((material) => (
                <button key={material.material_id} type="button" disabled={disabled} onClick={() => onUpdate({ libraryMaterial: material })} className={cn("flex w-full items-center justify-between gap-3 border-b px-4 py-3 text-left text-sm last:border-b-0", source.libraryMaterial?.material_id === material.material_id ? "bg-blue-50 text-primary dark:bg-blue-950/20" : "hover:bg-muted/50")}>
                  <span className="truncate font-medium">{material.filename}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{formatFileSize(material.size_bytes)}</span>
                </button>
              )) : <p className="px-4 py-6 text-center text-xs text-muted-foreground">{tx(locale, "没有匹配资料", "No matching material")}</p>}
            </div>
          </div>
        )}

        <div className="mt-3 grid gap-3 border-t pt-3 sm:grid-cols-[220px_minmax(0,1fr)]">
          <div>
            <p className="text-xs font-semibold text-muted-foreground">
              {source.sourceMode === "inline_text" ? tx(locale, "描述范围", "Description Scope") : tx(locale, "文件结构", "File Structure")}
            </p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <StructureButton active={source.structureMode === "organized"} disabled={disabled} onClick={() => onUpdate({ structureMode: "organized" })}>{source.sourceMode === "inline_text" ? tx(locale, "按题描述", "Per Question") : tx(locale, "已按题整理", "Organized")}</StructureButton>
              <StructureButton active={source.structureMode === "extract_from_source"} disabled={disabled} onClick={() => onUpdate({ structureMode: "extract_from_source" })}>{source.sourceMode === "inline_text" ? tx(locale, "整体规则", "Overall Rules") : tx(locale, "从原文提取", "Extract")}</StructureButton>
            </div>
          </div>
          {source.sourceMode === "inline_text" ? (
            <p className="self-end pb-1 text-xs leading-5 text-muted-foreground">
              {source.structureMode === "organized"
                ? tx(locale, "请在描述中写明题号；系统会分别匹配到对应题目。", "Include question numbers so each rule can be matched directly.")
                : tx(locale, "整体规则会由 SmarTAI 结合每道题的标答生成对应评分步骤。", "SmarTAI combines the overall rules with each question's reference answer to generate matching grading steps.")}
            </p>
          ) : source.structureMode === "extract_from_source" ? (
            <label className="grid gap-2 text-xs font-semibold text-muted-foreground">
              {tx(locale, "提取说明", "Extraction Hint")}
              <textarea value={source.extractionHint} disabled={disabled} maxLength={2000} onChange={(event) => onUpdate({ extractionHint: event.target.value })} placeholder={tx(locale, "例如：第 3 章习题 1–8，只提取正文中的题目与对应答案", "Example: Chapter 3, exercises 1–8; extract only the question text and matching reference answers")} className="min-h-[74px] resize-y rounded-[7px] border bg-card px-3 py-2 text-sm font-normal leading-5 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
            </label>
          ) : <p className="self-end pb-1 text-xs leading-5 text-muted-foreground">{tx(locale, "系统按明确题号匹配到同一道题。", "Content is matched by explicit question numbers.")}</p>}
        </div>

        {source.sourceMode === "upload" ? (
          <label className="mt-3 inline-flex items-center gap-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={source.saveToLibrary} disabled={disabled} onChange={(event) => onUpdate({ saveToLibrary: event.target.checked })} className="h-4 w-4 rounded border-border accent-primary" />
            {tx(locale, "同时保存到课程资料库", "Also save to course library")}
          </label>
        ) : null}
      </div>
    </article>
  );
}

function ModeButton({ active, disabled, onClick, icon, label }: { active: boolean; disabled: boolean; onClick: () => void; icon: ReactNode; label: string }) {
  return <button type="button" disabled={disabled} onClick={onClick} className={cn("inline-flex h-9 items-center justify-center gap-2 rounded-[6px] text-xs font-semibold transition", active ? "bg-card text-primary shadow-sm" : "text-muted-foreground hover:text-foreground")}>{icon}{label}</button>;
}

function StructureButton({ active, disabled, onClick, children }: { active: boolean; disabled: boolean; onClick: () => void; children: ReactNode }) {
  return <button type="button" disabled={disabled} onClick={onClick} className={cn("h-9 rounded-[7px] border px-2 text-xs font-semibold transition", active ? "border-primary bg-blue-50 text-primary dark:bg-blue-950/20" : "bg-card text-muted-foreground hover:bg-muted")}>{children}</button>;
}

function StartRequirementsDialog({
  open,
  needsByok,
  needsProblemSource,
  returnTo,
  routeState,
  locale,
  onClose,
  onGoToProblems,
}: {
  open: boolean;
  needsByok: boolean;
  needsProblemSource: boolean;
  returnTo: string;
  routeState: AddProblemsRouteState;
  locale: string;
  onClose: () => void;
  onGoToProblems: () => void;
}) {
  const byokActionRef = useRef<HTMLAnchorElement>(null);
  const problemActionRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => (byokActionRef.current ?? problemActionRef.current)?.focus());
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => { window.cancelAnimationFrame(frame); document.removeEventListener("keydown", onKey); };
  }, [onClose, open]);
  if (!open) return null;
  const requirementCount = Number(needsProblemSource) + Number(needsByok);
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/35 p-5" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-labelledby="start-requirements-title" className="w-full max-w-[480px] rounded-[10px] border bg-card p-6 shadow-xl">
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-blue-50 text-primary dark:bg-blue-950/50">
          {needsProblemSource ? <FileText aria-hidden="true" className="h-5 w-5" /> : <KeyRound aria-hidden="true" className="h-5 w-5" />}
        </span>
        <h2 id="start-requirements-title" className="mt-4 text-lg font-bold text-foreground">
          {tx(locale, `开始前还需要完成 ${requirementCount} 项`, `${requirementCount} requirement${requirementCount === 1 ? "" : "s"} remaining`)}
        </h2>
        <div className="mt-4 space-y-3">
          {needsProblemSource ? (
            <div className="rounded-[8px] border bg-slate-50 px-4 py-3 dark:bg-slate-950/30">
              <p className="text-sm font-semibold text-foreground">{tx(locale, "上传题目", "Add Questions")}</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{tx(locale, "至少选择一份题目文件，或从课程资料库选择题目来源。其他三类资料仍可留空。", "Choose at least one question file or a question source from the course library. The other material types may remain empty.")}</p>
            </div>
          ) : null}
          {needsByok ? (
            <div className="rounded-[8px] border bg-slate-50 px-4 py-3 dark:bg-slate-950/30">
              <p className="text-sm font-semibold text-foreground">{tx(locale, "启用 BYOK 模型", "Enable a BYOK Model")}</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{tx(locale, "题目识别、标答补全过程和评分标准生成需要模型；往返配置时当前草稿会保留。", "Question recognition and material generation require a model. Your draft is preserved while you configure BYOK.")}</p>
            </div>
          ) : null}
        </div>
        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:flex-wrap sm:justify-end">
          <button type="button" onClick={onClose} className="h-10 rounded-[8px] border px-4 text-sm font-semibold text-foreground hover:bg-muted">{tx(locale, "暂不处理", "Not Now")}</button>
          {needsProblemSource ? <button ref={problemActionRef} type="button" onClick={onGoToProblems} className="inline-flex h-10 items-center justify-center rounded-[8px] border px-4 text-sm font-semibold text-foreground hover:bg-muted">{tx(locale, "上传题目", "Add Questions")}</button> : null}
          {needsByok ? <Link ref={byokActionRef} to={`/settings/byok?returnTo=${encodeURIComponent(returnTo)}`} state={routeState} className="inline-flex h-10 items-center justify-center rounded-[8px] bg-primary px-4 text-sm font-semibold text-primary-foreground hover:opacity-90">{tx(locale, "前往 BYOK", "Open BYOK")}</Link> : null}
        </div>
      </div>
    </div>
  );
}

function createSourceDraft(role: PreparationSourceRole): SourceDraft {
  return {
    id: globalThis.crypto?.randomUUID?.() ?? `source-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    role,
    sourceMode: "upload",
    file: null,
    libraryScope: "course",
    librarySearch: "",
    libraryMaterial: null,
    inlineText: "",
    structureMode: "organized",
    extractionHint: "",
    saveToLibrary: false,
  };
}

function getRestoredDraft(state: unknown, taskId?: string) {
  const draft = (state as AddProblemsRouteState | null)?.questionPreparationDraft;
  return draft && draft.taskId === taskId && draft.sources?.length ? draft : null;
}

function sourceHasValue(source: SourceDraft) {
  if (source.sourceMode === "upload") return Boolean(source.file);
  if (source.sourceMode === "inline_text") return Boolean(source.inlineText?.trim());
  return Boolean(source.libraryMaterial);
}

function sourceSummary(source: SourceDraft, locale: string) {
  if (source.sourceMode === "upload" && source.file) return source.file.name;
  if (source.sourceMode === "library" && source.libraryMaterial) return source.libraryMaterial.filename;
  if (source.sourceMode === "inline_text" && source.inlineText?.trim()) return tx(locale, "自然语言评分标准", "Natural-language rubric");
  return roleMeta(source.role, locale).sourceLabel;
}

function sourceFileValidationError(
  file: File,
  role: PreparationSourceRole,
  acceptedExtensions: string[],
  locale: string,
) {
  const normalizedName = file.name.trim().toLowerCase();
  const extension = normalizedName.includes(".")
    ? `.${normalizedName.split(".").pop()}`
    : "";
  if (extension && acceptedExtensions.includes(extension)) return null;

  if (IMAGE_SOURCE_EXTENSIONS.has(extension)) {
    if (role === "programming_tests") {
      return tx(
        locale,
        "编程题测试资料不接受图片，请上传 PDF、TXT、Markdown 或 JSON。",
        "Programming-test materials do not accept images. Upload PDF, TXT, Markdown, or JSON.",
      );
    }
    return tx(
      locale,
      "当前模型配置未开放题目图片 OCR。请启用支持视觉识别的模型并刷新，或改用 PDF、TXT、Markdown。",
      "Question-image OCR is not available with the current model configuration. Enable a vision-capable model and refresh, or use PDF, TXT, or Markdown.",
    );
  }

  const formats = acceptedExtensions
    .map((item) => item.replace(/^\./, "").toUpperCase())
    .join(" / ");
  return tx(
    locale,
    `不支持 ${extension ? extension.slice(1).toUpperCase() : "无扩展名"} 文件。当前可用格式：${formats}。`,
    `${extension ? extension.slice(1).toUpperCase() : "Files without an extension are"} not supported. Available formats: ${formats}.`,
  );
}

function roleMeta(role: PreparationSourceRole, locale: string) {
  const zh = {
    problem: { title: "题目文件（必填）", shortTitle: "题目", sourceLabel: "题目来源", addLabel: "再添加一份题目", description: "上传题目正文，或从课程资料库选择题目来源。" },
    reference_answer: { title: "标答 / 解答（可选）", shortTitle: "标答", sourceLabel: "标答来源", addLabel: "再添加一份标答", description: "可只提供最终答案；SmarTAI 会补全可审核的解题过程。" },
    rubric: { title: "评分标准（可选）", shortTitle: "评分标准", sourceLabel: "评分标准来源", addLabel: "再添加一份评分标准", description: "可上传文件、从资料库选择或直接描述；评分步骤会与标答对应。" },
    programming_tests: { title: "编程题测试资料（可选）", shortTitle: "测试样例", sourceLabel: "测试资料来源", addLabel: "再添加一份测试资料", description: "仅编程题使用，支持输入、期望输出、解释与隐藏测试。" },
  } as const;
  const en = {
    problem: { title: "Question Files (Required)", shortTitle: "Questions", sourceLabel: "Question source", addLabel: "Add Another Question Source", description: "Upload question text or choose a source from the course library." },
    reference_answer: { title: "Reference Answers / Solutions (Optional)", shortTitle: "Reference Answers", sourceLabel: "Reference answer source", addLabel: "Add Another Reference Answer Source", description: "A final answer is enough; SmarTAI will expand it into a reviewable solution." },
    rubric: { title: "Grading Rubrics (Optional)", shortTitle: "Rubrics", sourceLabel: "Rubric source", addLabel: "Add Another Rubric Source", description: "Upload a file, choose one from the library, or describe rules that align with the reference-answer steps." },
    programming_tests: { title: "Programming Tests (Optional)", shortTitle: "Test Cases", sourceLabel: "Programming test source", addLabel: "Add Another Programming Test Source", description: "For programming questions only: inputs, expected outputs, explanations, and hidden tests." },
  } as const;
  return locale === "zh-CN" ? zh[role] : en[role];
}

function formatFileSize(value?: number | null) {
  if (!value || value <= 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function useDebouncedValue(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timeout);
  }, [delay, value]);
  return debounced;
}

function getPreparationRecoveryAction({
  info,
  failure,
  routeState,
  locale,
  onRetry,
  onRefresh,
  onOpenSource,
}: {
  info: ReturnType<typeof classifyRecoverableError>;
  failure: PreparationFailure;
  routeState: AddProblemsRouteState;
  locale: string;
  onRetry: () => void;
  onRefresh: () => void;
  onOpenSource: (role: PreparationSourceRole, sourceId?: string, clearLibrary?: boolean) => void;
}): RecoveryAction {
  if (info.actionKind === "byok" && info.actionHref) {
    return { label: info.actionLabel, href: info.actionHref, state: routeState };
  }
  if (info.actionKind === "reupload") {
    return {
      label: info.actionLabel,
      onClick: () => onOpenSource(failure.sourceRole ?? "problem", failure.sourceId),
    };
  }
  if (info.actionKind === "reselect") {
    return {
      label: info.actionLabel,
      onClick: () => onOpenSource(failure.sourceRole ?? "problem", failure.sourceId, true),
    };
  }
  if (info.actionKind === "refresh") {
    return { label: info.actionLabel, onClick: onRefresh };
  }
  return { label: info.actionLabel || tx(locale, "重新尝试", "Try again"), onClick: onRetry };
}

function validateScorePolicy(
  draft: ScorePolicyDraft,
  locale: string,
  maximumMaxScore: number,
): { value: QuestionScorePolicyInput; error: null } | { value: null; error: string } {
  if (draft.mode === "default_10") {
    return { value: { mode: "default_10" }, error: null };
  }
  if (draft.mode === "per_question") {
    const instruction = draft.perQuestionText.trim();
    if (!instruction) {
      return {
        value: null,
        error: tx(locale, "已选择“每题满分不同”，请按题号填写每题满分。", "Describe each maximum score by question number, or turn off “Different score per question”."),
      };
    }
    return {
      value: { mode: "per_question", perQuestionText: instruction },
      error: null,
    };
  }
  const maxScore = Number(draft.uniformMaxScore);
  if (!Number.isFinite(maxScore) || maxScore <= 0 || maxScore > maximumMaxScore) {
    return {
      value: null,
      error: tx(locale, `每题满分必须大于 0 且不超过 ${maximumMaxScore}。`, `The maximum score must be greater than 0 and no more than ${maximumMaxScore}.`),
    };
  }
  return {
    value: { mode: "uniform", uniformMaxScore: maxScore },
    error: null,
  };
}

function tx(locale: string, zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

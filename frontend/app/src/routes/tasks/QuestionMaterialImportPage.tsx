import { ArrowLeft, FileText, LoaderCircle, Search } from "lucide-react";
import {
  useBeforeUnload,
  useBlocker,
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  useExperts,
  usePreflightMaterialImport,
  useProblemSourceLibrary,
  useStartMaterialImport,
  useTask,
} from "@/api/hooks";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { materialImportText } from "@/lib/materialImportCopy";
import type { MaterialImportTarget, ProblemLibraryMaterial, ProblemSourceScope } from "@/types";

type ImportTarget = "rubric" | "answer" | "tests";
type SourceMode = "library" | "upload";
type StructureMode = "organized" | "extract_from_source";

const ACCEPTED_EXTENSIONS = [".pdf", ".txt", ".md", ".markdown"] as const;

export function QuestionMaterialImportPage() {
  const { taskId } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const allowLeaveRef = useRef(false);
  const taskQuery = useTask(taskId);
  const expertsQuery = useExperts();
  const preflightImport = usePreflightMaterialImport();
  const startImport = useStartMaterialImport();

  const [targets, setTargets] = useState<ImportTarget[]>(() => readInitialTargets(searchParams.get("targets")));
  const [sourceMode, setSourceMode] = useState<SourceMode>("library");
  const [scope, setScope] = useState<ProblemSourceScope>("course");
  const [librarySearch, setLibrarySearch] = useState("");
  const debouncedSearch = useDebouncedValue(librarySearch, 220);
  const [selectedMaterial, setSelectedMaterial] = useState<ProblemLibraryMaterial | null>(null);
  const [showLibraryPicker, setShowLibraryPicker] = useState(true);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [saveToLibrary, setSaveToLibrary] = useState(false);
  const [structureMode, setStructureMode] = useState<StructureMode>("organized");
  const [extractionHint, setExtractionHint] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [hasUserChanges, setHasUserChanges] = useState(false);

  const libraryQuery = useProblemSourceLibrary(
    taskId,
    scope,
    debouncedSearch,
    sourceMode === "library" && taskQuery.isSuccess,
  );
  const enabledExperts = useMemo(
    () => (expertsQuery.data ?? []).filter((expert) => expert.enabled),
    [expertsQuery.data],
  );

  useEffect(() => {
    if (selectedMaterial || !libraryQuery.data?.items.length || librarySearch.trim()) return;
    setSelectedMaterial(libraryQuery.data.items[0]);
    setShowLibraryPicker(false);
  }, [libraryQuery.data?.items, librarySearch, selectedMaterial]);

  const isSubmitting = preflightImport.isPending || startImport.isPending;
  const isDirty = hasUserChanges;
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    isDirty && !allowLeaveRef.current && currentLocation.pathname !== nextLocation.pathname
  ));
  useBeforeUnload(useCallback((event) => {
    if (!isDirty || allowLeaveRef.current) return;
    event.preventDefault();
  }, [isDirty]));

  const taskReady = taskQuery.data?.status === "problems_ready";
  const hasSource = sourceMode === "library" ? Boolean(selectedMaterial) : Boolean(selectedFile);
  const gateMessage = taskQuery.isError
    ? materialImportText(locale, "taskUnavailable")
    : taskQuery.isSuccess && !taskReady
      ? materialImportText(locale, "taskNotReady")
      : (expertsQuery.isSuccess && enabledExperts.length === 0) || expertsQuery.isError
        ? materialImportText(locale, "modelRequired")
        : null;
  const actionDisabled = isSubmitting
    || taskQuery.isLoading
    || expertsQuery.isLoading
    || Boolean(gateMessage)
    || targets.length === 0
    || !hasSource;
  const actionMessage = formError
    ?? gateMessage
    ?? (targets.length === 0 ? materialImportText(locale, "targetRequired") : null)
    ?? (!hasSource ? materialImportText(locale, "sourceRequired") : null);

  function markChanged() {
    setHasUserChanges(true);
    setFormError(null);
  }

  function toggleTarget(target: ImportTarget) {
    setTargets((current) => current.includes(target)
      ? current.filter((item) => item !== target)
      : [...current, target]);
    markChanged();
  }

  function changeSource(next: SourceMode) {
    if (next === sourceMode || isSubmitting) return;
    setSourceMode(next);
    markChanged();
  }

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!isAcceptedFile(file.name)) {
      setFormError(materialImportText(locale, "unsupportedFile"));
      return;
    }
    setSelectedFile(file);
    setSourceMode("upload");
    markChanged();
  }

  async function handlePrimaryAction() {
    setFormError(null);
    if (targets.length === 0) {
      setFormError(materialImportText(locale, "targetRequired"));
      return;
    }
    if (!hasSource) {
      setFormError(materialImportText(locale, "sourceRequired"));
      return;
    }

    if (!taskId || gateMessage) return;
    setHasUserChanges(true);
    try {
      const preflight = await preflightImport.mutateAsync({
        taskId,
        file: sourceMode === "upload" ? selectedFile : null,
        libraryMaterialId: sourceMode === "library" ? selectedMaterial?.material_id : null,
        targets: targets.map(toApiTarget),
        structureMode,
        extractionHint: structureMode === "extract_from_source" ? extractionHint : "",
        saveToLibrary: sourceMode === "upload" && saveToLibrary,
      });
      const started = await startImport.mutateAsync({ taskId, sourceToken: preflight.source_token });
      allowLeaveRef.current = true;
      setHasUserChanges(false);
      if (started.status === "already_done") {
        navigate(`/tasks/${taskId}/questions`, { replace: true });
      } else if (started.status === "plan_ready") {
        navigate(`/tasks/${taskId}/questions/import/review/${encodeURIComponent(started.job_id)}`, { replace: true });
      } else {
        navigate(`/tasks/${taskId}/questions/import/progress/${encodeURIComponent(started.job_id)}`, { replace: true });
      }
    } catch (error) {
      setFormError(localizeImportError(error, locale));
    }
  }

  return (
    <div className="w-full max-w-[1300px]">
      <div className="min-h-9">
        <h1 className="break-words text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
          {materialImportText(locale, "title")}
        </h1>
      </div>
      <NewTaskStepper currentStep={2} />

      <form
        className="mx-auto mt-[35px] min-h-[558px] w-full max-w-[900px] rounded-[10px] border bg-card px-6 pb-6 pt-7 sm:px-[49px] sm:pb-[28px] sm:pt-[38px]"
        onSubmit={(event) => {
          event.preventDefault();
          void handlePrimaryAction();
        }}
      >
        <fieldset disabled={isSubmitting}>
          <legend className="text-[18px] font-bold leading-6 text-foreground">
            {materialImportText(locale, "targetsTitle")}
          </legend>
          <div className="mt-5 flex flex-wrap gap-3 sm:gap-5">
            <TargetButton active={targets.includes("rubric")} onClick={() => toggleTarget("rubric")}>
              {materialImportText(locale, "targetRubric")}
            </TargetButton>
            <TargetButton active={targets.includes("answer")} onClick={() => toggleTarget("answer")}>
              {materialImportText(locale, "targetAnswer")}
            </TargetButton>
            <TargetButton active={targets.includes("tests")} wide onClick={() => toggleTarget("tests")}>
              {materialImportText(locale, "targetTests")}
            </TargetButton>
          </div>

          <section className="mt-9" aria-labelledby="material-import-source-title">
            <h2 id="material-import-source-title" className="text-[18px] font-bold leading-6 text-foreground">
              {materialImportText(locale, "sourceTitle")}
            </h2>
            <div className="mt-5 flex flex-wrap gap-3 sm:gap-5">
              <TargetButton active={sourceMode === "library"} wide onClick={() => changeSource("library")}>
                {materialImportText(locale, "sourceLibrary")}
              </TargetButton>
              <TargetButton active={sourceMode === "upload"} wide onClick={() => changeSource("upload")}>
                {materialImportText(locale, "sourceUpload")}
              </TargetButton>
            </div>

            {sourceMode === "library" ? (
              <LibrarySourcePicker
                locale={locale}
                scope={scope}
                query={librarySearch}
                selected={selectedMaterial}
                showPicker={showLibraryPicker}
                hasTaskCourse={Boolean(taskQuery.data?.course_id)}
                materials={libraryQuery.data?.items ?? []}
                isFetching={libraryQuery.isFetching}
                isError={libraryQuery.isError}
                onChangeClick={() => setShowLibraryPicker(true)}
                onQueryChange={(value) => {
                  setLibrarySearch(value);
                  setShowLibraryPicker(true);
                }}
                onRetry={() => void libraryQuery.refetch()}
                onScopeChange={(value) => {
                  setScope(value);
                  setSelectedMaterial(null);
                  setShowLibraryPicker(true);
                  markChanged();
                }}
                onSelect={(material) => {
                  setSelectedMaterial(material);
                  setShowLibraryPicker(false);
                  markChanged();
                }}
              />
            ) : (
              <UploadSourcePicker
                locale={locale}
                file={selectedFile}
                inputRef={fileInputRef}
                saveToLibrary={saveToLibrary}
                onChoose={() => fileInputRef.current?.click()}
                onFileChange={chooseFile}
                onSaveChange={(value) => {
                  setSaveToLibrary(value);
                  markChanged();
                }}
              />
            )}
          </section>

          <section className="mt-8" aria-labelledby="material-import-structure-title">
            <h2 id="material-import-structure-title" className="text-[18px] font-bold leading-6 text-foreground">
              {materialImportText(locale, "structureTitle")}
            </h2>
            <div className="mt-5 grid gap-4 sm:grid-cols-2 sm:gap-10">
              <StructureButton
                active={structureMode === "organized"}
                title={materialImportText(locale, "organized")}
                description={materialImportText(locale, "organizedDescription")}
                onClick={() => {
                  setStructureMode("organized");
                  markChanged();
                }}
              />
              <StructureButton
                active={structureMode === "extract_from_source"}
                title={materialImportText(locale, "extract")}
                description={materialImportText(locale, "extractDescription")}
                onClick={() => {
                  setStructureMode("extract_from_source");
                  markChanged();
                }}
              />
            </div>
            {structureMode === "extract_from_source" ? (
              <label className="mt-4 block text-[13px] font-semibold leading-5 text-foreground">
                {materialImportText(locale, "hintLabel")}
                <textarea
                  value={extractionHint}
                  maxLength={2000}
                  onChange={(event) => {
                    setExtractionHint(event.target.value);
                    markChanged();
                  }}
                  placeholder={materialImportText(locale, "hintPlaceholder")}
                  className="mt-2 min-h-[72px] w-full resize-y rounded-[8px] border bg-card px-3 py-2 text-[14px] font-normal leading-5 text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
                />
              </label>
            ) : null}
          </section>
        </fieldset>

        <div className="mt-5 flex min-h-10 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p id="material-import-action-message" role={formError ? "alert" : undefined} className={cn(
            "min-w-0 text-[13px] leading-5",
            formError ? "text-danger" : "text-muted-foreground",
          )}>
            {actionMessage ?? ""}
            {gateMessage === materialImportText(locale, "modelRequired") ? (
              <>{" "}<Link className="font-semibold text-primary hover:underline" to={`/settings/byok?returnTo=${encodeURIComponent(`/tasks/${taskId ?? ""}/questions/import`)}`}>{materialImportText(locale, "openByok")}</Link></>
            ) : null}
          </p>
          <button
            type="submit"
            disabled={actionDisabled}
            aria-describedby="material-import-action-message"
            className="inline-flex h-10 w-full shrink-0 items-center justify-center rounded-[8px] bg-primary px-4 text-[15px] font-semibold leading-[19px] text-primary-foreground outline-none transition hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 sm:w-[230px]"
          >
            {isSubmitting ? <LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" /> : null}
            {materialImportText(locale, preflightImport.isPending ? "checking" : startImport.isPending ? "starting" : "start")}
          </button>
        </div>
      </form>
      <div className="mx-auto mt-4 w-full max-w-[900px]">
        <Link
          to={`/tasks/${taskId ?? ""}/questions`}
          className="inline-flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          {materialImportText(locale, "backToOverview")}
        </Link>
      </div>

      {blocker.state === "blocked" ? (
        <UnsavedChangesDialog
          title={materialImportText(locale, "leaveTitle")}
          description={materialImportText(locale, "leaveDescription")}
          stayLabel={materialImportText(locale, "stay")}
          leaveLabel={materialImportText(locale, "leave")}
          onStay={() => blocker.reset()}
          onLeave={() => blocker.proceed()}
        />
      ) : null}
    </div>
  );
}

function TargetButton({ active, children, onClick, wide = false }: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
  wide?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "h-[30px] rounded-full px-4 text-[13px] font-semibold leading-[18px] outline-none transition focus-visible:ring-2 focus-visible:ring-ring",
        wide ? "min-w-[130px]" : "min-w-[80px]",
        active ? "bg-[#DBE8FF] text-primary" : "bg-muted/70 text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function StructureButton({ active, title, description, onClick }: {
  active: boolean;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "min-h-[86px] rounded-[8px] border bg-card px-5 py-4 text-left outline-none transition focus-visible:ring-2 focus-visible:ring-ring",
        active ? "border-primary ring-1 ring-primary" : "hover:border-slate-300",
      )}
    >
      <span className="block text-[15px] font-bold leading-5 text-foreground">{title}</span>
      <span className="mt-2 block text-[12px] leading-[17px] text-muted-foreground">{description}</span>
    </button>
  );
}

function LibrarySourcePicker({
  locale,
  scope,
  query,
  selected,
  showPicker,
  hasTaskCourse,
  materials,
  isFetching,
  isError,
  onChangeClick,
  onQueryChange,
  onRetry,
  onScopeChange,
  onSelect,
}: {
  locale: "zh-CN" | "en-US";
  scope: ProblemSourceScope;
  query: string;
  selected: ProblemLibraryMaterial | null;
  showPicker: boolean;
  hasTaskCourse: boolean;
  materials: ProblemLibraryMaterial[];
  isFetching: boolean;
  isError: boolean;
  onChangeClick: () => void;
  onQueryChange: (value: string) => void;
  onRetry: () => void;
  onScopeChange: (scope: ProblemSourceScope) => void;
  onSelect: (material: ProblemLibraryMaterial) => void;
}) {
  const librarySearch = useImeSafeQuery({ value: query, onCommit: onQueryChange });

  if (selected && !showPicker) {
    return (
      <div className="mt-5 flex min-h-[78px] items-center justify-between gap-4 rounded-[8px] border bg-slate-50 px-5 dark:bg-slate-900/40">
        <p className="min-w-0 truncate text-[14px] text-foreground" title={selected.filename}>
          {materialImportText(locale, "selectedPrefix")}{selected.filename}
        </p>
        <button type="button" className="h-[30px] min-w-[80px] rounded-full bg-[#DBE8FF] px-4 text-[13px] font-semibold text-primary" onClick={onChangeClick}>
          {materialImportText(locale, "change")}
        </button>
      </div>
    );
  }

  return (
    <div className="mt-5 overflow-hidden rounded-[8px] border">
      <div className="flex flex-col gap-2 border-b p-2.5 sm:flex-row sm:items-center">
        <div className="flex shrink-0 gap-2">
          <button type="button" aria-pressed={scope === "course"} onClick={() => onScopeChange("course")} className={cn("h-8 rounded-[7px] px-3 text-xs font-semibold", scope === "course" ? "bg-[#DBE8FF] text-primary" : "bg-muted text-muted-foreground")}>{hasTaskCourse ? materialImportText(locale, "currentCourse") : materialImportText(locale, "uncategorized")}</button>
          <button type="button" aria-pressed={scope === "all"} onClick={() => onScopeChange("all")} className={cn("h-8 rounded-[7px] px-3 text-xs font-semibold", scope === "all" ? "bg-[#DBE8FF] text-primary" : "bg-muted text-muted-foreground")}>{materialImportText(locale, "allMine")}</button>
        </div>
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">{materialImportText(locale, "librarySearch")}</span>
          <Search aria-hidden="true" className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input value={librarySearch.draftValue} onBlur={librarySearch.handleBlur} onChange={librarySearch.handleChange} onCompositionEnd={librarySearch.handleCompositionEnd} onCompositionStart={librarySearch.handleCompositionStart} placeholder={materialImportText(locale, "librarySearchPlaceholder")} className="h-8 w-full rounded-[7px] border bg-card pl-9 pr-3 text-[13px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
        </label>
      </div>
      <div className="max-h-[116px] overflow-y-auto">
        {isError ? (
          <div className="flex min-h-14 items-center justify-center gap-2 px-4 text-xs text-danger">{materialImportText(locale, "libraryError")}<button type="button" className="font-semibold text-primary hover:underline" onClick={onRetry}>{materialImportText(locale, "refresh")}</button></div>
        ) : isFetching && materials.length === 0 ? (
          <div className="flex min-h-14 items-center justify-center"><LoaderCircle className="h-4 w-4 animate-spin text-muted-foreground" /></div>
        ) : materials.length === 0 ? (
          <div className="flex min-h-14 items-center justify-center px-4 text-xs text-muted-foreground">{materialImportText(locale, "libraryEmpty")}</div>
        ) : (
          <ul className="divide-y">
            {materials.map((material) => (
              <li key={material.material_id}>
                <button type="button" onClick={() => onSelect(material)} className="flex min-h-11 w-full items-center gap-3 px-4 py-2 text-left outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                  <FileText aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{material.filename}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{formatFileSize(material.size_bytes ?? 0)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function UploadSourcePicker({ locale, file, inputRef, saveToLibrary, onChoose, onFileChange, onSaveChange }: {
  locale: "zh-CN" | "en-US";
  file: File | null;
  inputRef: React.RefObject<HTMLInputElement | null>;
  saveToLibrary: boolean;
  onChoose: () => void;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onSaveChange: (value: boolean) => void;
}) {
  return (
    <div className="mt-5 rounded-[8px] border bg-slate-50 px-5 py-4 dark:bg-slate-900/40">
      <div className="flex min-h-10 items-center justify-between gap-4">
        <p className="min-w-0 truncate text-[14px] text-foreground" title={file?.name}>
          {file ? `${materialImportText(locale, "selectedPrefix")}${file.name} · ${formatFileSize(file.size)}` : materialImportText(locale, "noSource")}
        </p>
        <button type="button" className="h-[30px] min-w-[90px] rounded-full bg-[#DBE8FF] px-4 text-[13px] font-semibold text-primary" onClick={onChoose}>
          {file ? materialImportText(locale, "change") : materialImportText(locale, "chooseFile")}
        </button>
        <input ref={inputRef} type="file" className="sr-only" accept=".pdf,.txt,.md,.markdown,application/pdf,text/plain,text/markdown" onChange={onFileChange} />
      </div>
      <label className="mt-3 inline-flex items-center gap-2 text-[12px] text-muted-foreground">
        <input type="checkbox" checked={saveToLibrary} onChange={(event) => onSaveChange(event.target.checked)} className="h-4 w-4 rounded border-border accent-primary" />
        {materialImportText(locale, "saveToLibrary")}
      </label>
    </div>
  );
}

function readInitialTargets(value: string | null): ImportTarget[] {
  if (!value) return ["rubric", "answer"];
  const tokens = new Set(value.split(",").map((item) => item.trim().toLowerCase()));
  const targets: ImportTarget[] = [];
  if (tokens.has("rubric") || tokens.has("criterion")) targets.push("rubric");
  if (tokens.has("answer") || tokens.has("reference_answer")) targets.push("answer");
  if (tokens.has("tests") || tokens.has("test_cases")) targets.push("tests");
  return targets.length ? targets : ["rubric", "answer"];
}

function toApiTarget(target: ImportTarget): MaterialImportTarget {
  if (target === "rubric") return "criterion";
  if (target === "answer") return "reference_answer";
  return "test_cases";
}

function localizeImportError(error: unknown, locale: "zh-CN" | "en-US") {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(normalized) ?? "";
  const known: Record<string, [string, string]> = {
    material_import_requires_problems_ready: ["请先完成题目识别与准备。", "Finish question recognition and preparation first."],
    material_import_source_changed: ["课程资料已发生变化，请重新选择。", "The Course Library material changed. Select it again."],
    material_import_source_unavailable: ["资料来源已失效，请重新选择。", "The material source expired. Select it again."],
    task_workflow_changed: ["任务内容已发生变化，请刷新后重新导入。", "The task changed. Refresh and start the import again."],
    stale_revision: ["任务内容已发生变化，请刷新后重新导入。", "The task changed. Refresh and start the import again."],
    different_material_import_running: ["当前已有另一项资料导入正在进行。", "Another material import is already running."],
  };
  if (code && known[code]) return locale === "zh-CN" ? known[code][0] : known[code][1];
  if (normalized.status === 503) return materialImportText(locale, "modelRequired");
  if (normalized.status === 0) return materialImportText(locale, "genericError");
  return normalized.message || materialImportText(locale, "genericError");
}

function isAcceptedFile(filename: string) {
  const normalized = filename.trim().toLowerCase();
  return ACCEPTED_EXTENSIONS.some((extension) => normalized.endsWith(extension));
}

function formatFileSize(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function useDebouncedValue<T>(value: T, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}

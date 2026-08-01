import { ArrowLeft, LoaderCircle, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useBeforeUnload, useBlocker, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { useAICompletionPreflight, useExperts, useStartAICompletion } from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { aiCompletionText } from "@/lib/aiCompletionCopy";
import { cn } from "@/lib/cn";
import type { AICompletionMissingTarget, AICompletionTarget } from "@/types";

const TEST_CASE_OPTIONS = [3, 4, 5, 6, 8, 10, 12] as const;

export function QuestionAICompletionPage() {
  const { taskId } = useParams();
  const [searchParams] = useSearchParams();
  const { locale } = useI18n();
  const navigate = useNavigate();
  const preflightQuery = useAICompletionPreflight(taskId);
  const expertsQuery = useExperts();
  const startCompletion = useStartAICompletion();
  const initializedKeyRef = useRef<string | null>(null);
  const initialSelectionRef = useRef<string[]>([]);
  const allowLeaveRef = useRef(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [testCaseCount, setTestCaseCount] = useState(6);
  const [actionError, setActionError] = useState<string | null>(null);

  const preflight = preflightQuery.data;
  const enabledExperts = useMemo(
    () => (expertsQuery.data ?? []).filter((expert) => expert.enabled),
    [expertsQuery.data],
  );
  const sortedTargets = useMemo(
    () => [...(preflight?.missing_targets ?? [])].sort(compareMissingTargets),
    [preflight?.missing_targets],
  );

  useEffect(() => {
    if (!preflight) return;
    const key = `${preflight.workflow_revision}:${preflight.missing_targets.map((target) => target.target_id).join("|")}`;
    if (initializedKeyRef.current === key) return;
    initializedKeyRef.current = key;
    const requestedQuestion = searchParams.get("q_id")?.trim();
    const requestedTarget = normalizeTarget(searchParams.get("target"));
    const hasFocusedRequest = Boolean(requestedQuestion || requestedTarget);
    const focused = preflight.missing_targets.filter((target) => (
      (!requestedQuestion || target.q_id === requestedQuestion)
      && (!requestedTarget || target.target === requestedTarget)
    ));
    const initial = (hasFocusedRequest ? focused : preflight.missing_targets).map((target) => target.target_id);
    setSelectedIds(initial);
    initialSelectionRef.current = initial;
  }, [preflight, searchParams]);

  const isDirty = initializedKeyRef.current !== null
    && !sameIdSet(selectedIds, initialSelectionRef.current);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    isDirty && !allowLeaveRef.current && (
      currentLocation.pathname !== nextLocation.pathname
      || currentLocation.search !== nextLocation.search
    )
  ));
  useBeforeUnload(useCallback((event) => {
    if (!isDirty || allowLeaveRef.current) return;
    event.preventDefault();
  }, [isDirty]));

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const includesTests = sortedTargets.some((target) => (
    target.target === "test_cases" && selectedSet.has(target.target_id)
  ));
  const modelsReady = expertsQuery.isSuccess && enabledExperts.length > 0;
  const actionDisabled = !preflight
    || selectedIds.length === 0
    || !modelsReady
    || startCompletion.isPending;

  function toggleTarget(targetId: string, checked: boolean) {
    setActionError(null);
    setSelectedIds((current) => checked
      ? [...new Set([...current, targetId])]
      : current.filter((id) => id !== targetId));
  }

  async function handleConfirm() {
    if (!taskId || !preflight || selectedIds.length === 0) {
      setActionError(aiCompletionText(locale, "selectionRequired"));
      return;
    }
    if (!modelsReady) return;
    setActionError(null);
    try {
      const result = await startCompletion.mutateAsync({
        taskId,
        targetIds: selectedIds,
        expectedWorkflowRevision: preflight.workflow_revision,
        ...(includesTests ? { testCaseCount } : {}),
      });
      allowLeaveRef.current = true;
      navigate(`/tasks/${taskId}/questions/ai-complete/progress/${encodeURIComponent(result.job_id)}`, { replace: true });
    } catch (error) {
      const normalized = normalizeAPIError(error);
      const code = getAPIErrorCode(normalized) ?? "";
      if (["stale_revision", "unknown_ai_completion_target"].includes(code)) {
        const refreshed = await preflightQuery.refetch();
        setActionError(refreshed.isSuccess && refreshed.data
          ? aiCompletionText(locale, "scopeRefreshed")
          : localizeStartError(normalized, locale));
        return;
      }
      setActionError(localizeStartError(normalized, locale));
    }
  }

  return (
    <div className="min-w-0 w-full max-w-[1300px]">
      <h1 className="min-h-9 break-words text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {aiCompletionText(locale, "title")}
      </h1>
      <NewTaskStepper currentStep={2} />

      <section className="mx-auto mt-[35px] w-full max-w-[900px] rounded-[10px] border bg-card px-5 pb-6 pt-7 sm:flex sm:h-[558px] sm:min-h-0 sm:flex-col sm:overflow-hidden sm:px-[49px] sm:pb-[28px] sm:pt-[32px]">
        <header>
          <h2 className="text-[18px] font-bold leading-6 text-foreground">
            {aiCompletionText(locale, "cardTitle")}
          </h2>
          <p className="mt-2 max-w-[720px] text-[12px] leading-5 text-muted-foreground">
            {aiCompletionText(locale, "cardDescription")}
          </p>
          {preflight ? <p className="mt-1 text-[11px] font-medium text-foreground">{aiCompletionText(locale, "summaryQuestions")} {preflight.summary.question_count} · {aiCompletionText(locale, "summaryMissing")} {preflight.summary.missing_count}</p> : null}
        </header>

        {preflightQuery.isLoading ? (
          <div className="flex min-h-[360px] items-center justify-center" aria-busy="true">
            <LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" />
          </div>
        ) : preflightQuery.isError ? (
          <CompletionLoadError locale={locale} onRetry={() => void preflightQuery.refetch()} />
        ) : preflight && sortedTargets.length === 0 ? (
          <div className="flex min-h-[360px] flex-col items-center justify-center px-2 text-center">
            <Sparkles aria-hidden="true" className="h-8 w-8 text-accent" />
            <h3 className="mt-4 text-base font-bold text-foreground">{aiCompletionText(locale, "noMissingTitle")}</h3>
            <p className="mt-2 max-w-md text-xs leading-5 text-muted-foreground">{aiCompletionText(locale, "noMissingDescription")}</p>
            <Link to={`/tasks/${taskId ?? ""}/questions`} className="mt-5 inline-flex h-9 items-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted">
              {aiCompletionText(locale, "backOverview")}
            </Link>
          </div>
        ) : preflight ? (
          <>
            <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <TargetCount target="criterion" count={preflight.summary.by_target.criterion} locale={locale} />
              <TargetCount target="reference_answer" count={preflight.summary.by_target.reference_answer} locale={locale} />
              <TargetCount target="solution_code" count={preflight.summary.by_target.solution_code} locale={locale} />
              <TargetCount target="test_cases" count={preflight.summary.by_target.test_cases} locale={locale} />
            </div>

            <div className="mt-5 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <h3 className="text-[15px] font-bold leading-5 text-foreground">{aiCompletionText(locale, "scopeTitle")}</h3>
                <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{aiCompletionText(locale, "scopeDescription")}</p>
              </div>
              <div className="flex shrink-0 gap-2">
                <button type="button" onClick={() => { setActionError(null); setSelectedIds(sortedTargets.map((target) => target.target_id)); }} className="h-8 rounded-[6px] px-3 text-xs font-semibold text-primary hover:bg-primary/10">
                  {aiCompletionText(locale, "selectAll")}
                </button>
                <button type="button" onClick={() => { setActionError(null); setSelectedIds([]); }} className="h-8 rounded-[6px] px-3 text-xs font-semibold text-muted-foreground hover:bg-muted">
                  {aiCompletionText(locale, "clearAll")}
                </button>
              </div>
            </div>

            <TargetMatrix
              targets={sortedTargets}
              selectedSet={selectedSet}
              locale={locale}
              disabled={startCompletion.isPending}
              onToggle={toggleTarget}
            />

            {includesTests ? (
              <label className="mt-3 flex min-w-0 flex-col gap-2 text-xs font-medium text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
                <span>{aiCompletionText(locale, "testsPerQuestion")}</span>
                <select value={testCaseCount} onChange={(event) => setTestCaseCount(Number(event.target.value))} className="h-9 w-full rounded-[7px] border bg-background px-3 text-sm text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15 sm:w-[120px]">
                  {TEST_CASE_OPTIONS.map((count) => <option key={count} value={count}>{count}{aiCompletionText(locale, "testsCountSuffix")}</option>)}
                </select>
              </label>
            ) : null}

            <div className="mt-3 rounded-[8px] border border-blue-100 bg-blue-50/60 px-3 py-2 dark:border-blue-900 dark:bg-blue-950/20 sm:flex sm:items-center sm:gap-2">
              <p className="shrink-0 text-[11px] font-bold text-primary">{aiCompletionText(locale, "missingOnlyTitle")}</p>
              <p className="mt-1 text-[10px] leading-4 text-muted-foreground sm:mt-0 sm:truncate" title={aiCompletionText(locale, "missingOnlyDescription")}>{aiCompletionText(locale, "missingOnlyDescription")}</p>
            </div>

            <ModelGate
              locale={locale}
              loading={expertsQuery.isLoading}
              unavailable={expertsQuery.isError}
              missing={expertsQuery.isSuccess && enabledExperts.length === 0}
              returnTo={`/tasks/${taskId ?? ""}/questions/ai-complete`}
            />

            {actionError ? <p role="alert" className="mt-2 text-xs leading-5 text-danger">{actionError}</p> : null}

            <footer className="mt-4 flex shrink-0 flex-col-reverse gap-3 sm:mt-auto sm:flex-row sm:items-end sm:justify-between sm:pt-3">
              <Link to={`/tasks/${taskId ?? ""}/questions`} className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-[8px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted sm:w-auto">
                <ArrowLeft aria-hidden="true" className="h-4 w-4" />
                {aiCompletionText(locale, "backOverview")}
              </Link>
              <div className="min-w-0 sm:text-right">
                <p className="mb-2 text-xs text-muted-foreground">{aiCompletionText(locale, "selectedPrefix")}{selectedIds.length}{aiCompletionText(locale, "selectedSuffix")}</p>
                <button type="button" disabled={actionDisabled} onClick={() => void handleConfirm()} className="inline-flex h-10 w-full items-center justify-center rounded-[8px] bg-primary px-6 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 sm:w-[250px]">
                  {startCompletion.isPending ? <LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles aria-hidden="true" className="mr-2 h-4 w-4" />}
                  {aiCompletionText(locale, startCompletion.isPending ? "confirming" : "confirm")}
                </button>
              </div>
            </footer>
          </>
        ) : null}
      </section>

      {blocker.state === "blocked" ? (
        <UnsavedChangesDialog
          title={aiCompletionText(locale, "leaveTitle")}
          description={aiCompletionText(locale, "leaveDescription")}
          stayLabel={aiCompletionText(locale, "stay")}
          leaveLabel={aiCompletionText(locale, "leave")}
          onStay={() => blocker.reset()}
          onLeave={() => blocker.proceed()}
        />
      ) : null}
    </div>
  );
}

function TargetMatrix({ targets, selectedSet, locale, disabled, onToggle }: {
  targets: AICompletionMissingTarget[];
  selectedSet: Set<string>;
  locale: Locale;
  disabled: boolean;
  onToggle: (targetId: string, checked: boolean) => void;
}) {
  return (
    <div className="mt-2 min-h-[140px] max-h-[180px] min-w-0 overflow-auto overscroll-contain rounded-[8px] border sm:min-h-0 sm:flex-1">
      <table className="w-full min-w-[590px] border-collapse text-left text-[12px]">
        <thead className="sticky top-0 z-10 bg-muted/95 font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <th className="w-12 px-3 py-2.5"><span className="sr-only">{aiCompletionText(locale, "selectAll")}</span></th>
            <th className="w-[140px] px-2 py-2.5">{aiCompletionText(locale, "columnQuestion")}</th>
            <th className="w-[180px] px-2 py-2.5">{aiCompletionText(locale, "columnType")}</th>
            <th className="px-2 py-2.5">{aiCompletionText(locale, "columnTarget")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {targets.map((target) => (
            <tr key={target.target_id} className={cn("h-11 transition-colors", selectedSet.has(target.target_id) ? "bg-blue-50/40 dark:bg-blue-950/10" : "bg-card")}>
              <td className="px-3 py-2">
                <input type="checkbox" checked={selectedSet.has(target.target_id)} disabled={disabled} onChange={(event) => onToggle(target.target_id, event.target.checked)} className="h-4 w-4 rounded border-border accent-primary disabled:opacity-50" aria-label={`${target.question_number} ${targetLabel(target.target, locale)}`} />
              </td>
              <td className="px-2 py-2 font-semibold text-foreground">{target.question_number || target.q_id}</td>
              <td className="max-w-[180px] truncate px-2 py-2 text-muted-foreground" title={target.question_type}>{target.question_type || "—"}</td>
              <td className="px-2 py-2 font-medium text-foreground">{targetLabel(target.target, locale)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SummaryTile({ label, value }: { label: string; value: number }) {
  return <div className="flex min-w-0 items-center justify-between gap-2 rounded-[7px] border bg-background px-3 py-2"><p className="truncate text-[10px] text-muted-foreground" title={label}>{label}</p><p className="shrink-0 text-sm font-bold text-foreground">{value}</p></div>;
}

function TargetCount({ target, count, locale }: { target: AICompletionTarget; count: number; locale: Locale }) {
  return <SummaryTile label={targetLabel(target, locale)} value={count} />;
}

function ModelGate({ locale, loading, unavailable, missing, returnTo }: { locale: Locale; loading: boolean; unavailable: boolean; missing: boolean; returnTo: string }) {
  if (!loading && !unavailable && !missing) return null;
  const titleKey = loading ? "modelLoading" : unavailable ? "modelUnavailableTitle" : "modelMissingTitle";
  const descriptionKey = unavailable ? "modelUnavailableDescription" : "modelMissingDescription";
  return (
    <div className="mt-2 flex flex-col gap-2 rounded-[8px] border border-amber-200 bg-amber-50 px-3 py-2 dark:border-amber-900 dark:bg-amber-950/20 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className="text-xs font-bold text-amber-800 dark:text-amber-200">{aiCompletionText(locale, titleKey)}</p>
        {!loading ? <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{aiCompletionText(locale, descriptionKey)}</p> : null}
      </div>
      {!loading ? <Link to={`/settings/byok?returnTo=${encodeURIComponent(returnTo)}`} className="inline-flex h-9 shrink-0 items-center justify-center rounded-[7px] bg-primary px-4 text-xs font-semibold text-primary-foreground hover:opacity-90">{aiCompletionText(locale, "configureModels")}</Link> : null}
    </div>
  );
}

function CompletionLoadError({ locale, onRetry }: { locale: Locale; onRetry: () => void }) {
  return (
    <div className="flex min-h-[360px] flex-col items-center justify-center px-2 text-center">
      <h3 className="text-base font-bold text-foreground">{aiCompletionText(locale, "loadErrorTitle")}</h3>
      <p className="mt-2 max-w-md text-xs leading-5 text-muted-foreground">{aiCompletionText(locale, "loadErrorDescription")}</p>
      <button type="button" onClick={onRetry} className="mt-5 h-9 rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground hover:bg-muted">{aiCompletionText(locale, "refresh")}</button>
    </div>
  );
}

function targetLabel(target: AICompletionTarget, locale: Locale) {
  const keys = {
    criterion: "targetCriterion",
    reference_answer: "targetAnswer",
    solution_code: "targetCode",
    test_cases: "targetTests",
  } as const;
  return aiCompletionText(locale, keys[target]);
}

function compareMissingTargets(left: AICompletionMissingTarget, right: AICompletionMissingTarget) {
  return (left.question_number || left.q_id).localeCompare(right.question_number || right.q_id, undefined, { numeric: true })
    || targetOrder(left.target) - targetOrder(right.target);
}

function targetOrder(target: AICompletionTarget) {
  return ["criterion", "reference_answer", "solution_code", "test_cases"].indexOf(target);
}

function normalizeTarget(value: string | null): AICompletionTarget | null {
  return value && ["criterion", "reference_answer", "solution_code", "test_cases"].includes(value)
    ? value as AICompletionTarget
    : null;
}

function localizeStartError(error: unknown, locale: Locale) {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(normalized) ?? "";
  const known = {
    ai_completion_requires_problems_ready: "errorTaskNotReady",
    ai_completion_targets_required: "selectionRequired",
    unknown_ai_completion_target: "errorUnknownTarget",
    stale_revision: "errorStale",
    workflow_busy: "errorBusy",
    invalid_state: "errorTaskNotReady",
  } as const;
  if (code in known) return aiCompletionText(locale, known[code as keyof typeof known]);
  if (normalized.status === 503) return aiCompletionText(locale, "errorModel");
  if (normalized.status === 0) return aiCompletionText(locale, "startErrorGeneric");
  return normalized.message || aiCompletionText(locale, "startErrorGeneric");
}

function sameIdSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((id) => rightSet.has(id));
}

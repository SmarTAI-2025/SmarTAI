import { ArrowLeft, LoaderCircle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useBeforeUnload, useBlocker, useNavigate, useParams } from "react-router-dom";
import { useApplyMaterialImport, useMaterialImport, useTask } from "@/api/hooks";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { EmptyState } from "@/components/ui/EmptyState";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { materialImportText } from "@/lib/materialImportCopy";
import type { MaterialImportCandidate, MaterialImportTarget, ProblemInfo } from "@/types";

export function QuestionMaterialImportReviewPage() {
  const { taskId, jobId } = useParams();
  const { locale } = useI18n();
  const navigate = useNavigate();
  const importQuery = useMaterialImport(taskId, jobId);
  const taskQuery = useTask(taskId);
  const applyImport = useApplyMaterialImport();
  const initializedJobRef = useRef<string | null>(null);
  const allowLeaveRef = useRef(false);
  const [acceptedIds, setAcceptedIds] = useState<string[]>([]);
  const [overwriteIds, setOverwriteIds] = useState<string[]>([]);
  const [initialAcceptedIds, setInitialAcceptedIds] = useState<string[]>([]);
  const [initialOverwriteIds, setInitialOverwriteIds] = useState<string[]>([]);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [invalidPlanReason, setInvalidPlanReason] = useState<string | null>(null);
  const plan = importQuery.data;
  const problems = taskQuery.data?.problem_data ?? {};
  const planLoadError = importQuery.error ? normalizeAPIError(importQuery.error) : null;
  const planLoadErrorCode = getAPIErrorCode(planLoadError);
  const planExpired = planLoadError?.status === 410
    || planLoadErrorCode === "material_import_plan_expired";

  useEffect(() => {
    if (!taskId || !jobId || !plan) return;
    if (plan.status === "running") {
      navigate(`/tasks/${taskId}/questions/import/progress/${encodeURIComponent(jobId)}`, { replace: true });
    } else if (plan.status === "applied") {
      navigate(`/tasks/${taskId}/questions`, { replace: true });
    }
  }, [jobId, navigate, plan, taskId]);

  useEffect(() => {
    if (!jobId || plan?.status !== "ready" || initializedJobRef.current === jobId) return;
    initializedJobRef.current = jobId;
    const defaultAccepted = plan.candidates
      .filter((candidate) => !candidate.would_overwrite && candidate.match_status === "exact")
      .map((candidate) => candidate.candidate_id);
    setAcceptedIds(defaultAccepted);
    setOverwriteIds([]);
    setInitialAcceptedIds(defaultAccepted);
    setInitialOverwriteIds([]);
  }, [jobId, plan]);

  const isSelectionDirty = initializedJobRef.current === jobId
    && !invalidPlanReason
    && (!sameIdSet(acceptedIds, initialAcceptedIds) || !sameIdSet(overwriteIds, initialOverwriteIds));
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    isSelectionDirty && !allowLeaveRef.current && currentLocation.pathname !== nextLocation.pathname
  ));
  useBeforeUnload(useCallback((event) => {
    if (!isSelectionDirty || allowLeaveRef.current) return;
    event.preventDefault();
  }, [isSelectionDirty]));

  const selectedCount = acceptedIds.length;
  const sortedCandidates = useMemo(
    () => [...(plan?.candidates ?? [])].sort((left, right) => {
      const leftNumber = problems[left.q_id]?.number ?? left.q_id;
      const rightNumber = problems[right.q_id]?.number ?? right.q_id;
      return leftNumber.localeCompare(rightNumber, locale, { numeric: true })
        || targetOrder(left.target) - targetOrder(right.target);
    }),
    [locale, plan?.candidates, problems],
  );

  function toggleCandidate(candidate: MaterialImportCandidate, checked: boolean) {
    setApplyError(null);
    setAcceptedIds((current) => checked
      ? [...new Set([...current, candidate.candidate_id])]
      : current.filter((id) => id !== candidate.candidate_id));
    if (!checked) {
      setOverwriteIds((current) => current.filter((id) => id !== candidate.candidate_id));
    }
  }

  function toggleOverwrite(candidate: MaterialImportCandidate, checked: boolean) {
    setApplyError(null);
    setOverwriteIds((current) => checked
      ? [...new Set([...current, candidate.candidate_id])]
      : current.filter((id) => id !== candidate.candidate_id));
    setAcceptedIds((current) => checked
      ? [...new Set([...current, candidate.candidate_id])]
      : current.filter((id) => id !== candidate.candidate_id));
  }

  async function handleApply() {
    if (!taskId || !jobId || !plan || plan.status !== "ready" || selectedCount === 0 || !taskQuery.isSuccess || invalidPlanReason) return;
    setApplyError(null);
    try {
      await applyImport.mutateAsync({
        taskId,
        jobId,
        acceptedCandidateIds: acceptedIds,
        overwriteCandidateIds: overwriteIds,
        expectedWorkflowRevision: plan.workflow_revision,
      });
      allowLeaveRef.current = true;
      navigate(`/tasks/${taskId}/questions`, { replace: true });
    } catch (error) {
      const normalized = normalizeAPIError(error);
      const code = getAPIErrorCode(normalized) ?? "";
      const invalidPlanMessages: Record<string, [string, string]> = {
        stale_workflow_revision: ["题目资料已发生变化，当前匹配计划不能继续使用。", "Question materials changed, so this match plan can no longer be applied."],
        stale_revision: ["题目资料已发生变化，当前匹配计划不能继续使用。", "Question materials changed, so this match plan can no longer be applied."],
        workflow_busy: ["任务正在执行其他操作，当前匹配计划不能继续使用。", "The task is busy, so this match plan can no longer be applied."],
        plan_superseded: ["当前匹配计划已被新计划替代。", "This match plan was superseded by a newer plan."],
        material_import_plan_expired: ["当前匹配计划已过期，需要重新选择资料并匹配。", "This match plan expired. Choose the material and match it again."],
      };
      if (normalized.status === 410 && !code) {
        setInvalidPlanReason(locale === "zh-CN"
          ? "当前匹配计划已过期，需要重新选择资料并匹配。"
          : "This match plan expired. Choose the material and match it again.");
        setApplyError(null);
        return;
      }
      if (invalidPlanMessages[code]) {
        setInvalidPlanReason(locale === "zh-CN" ? invalidPlanMessages[code][0] : invalidPlanMessages[code][1]);
        setApplyError(null);
      } else {
        setApplyError(normalized.message);
      }
    }
  }

  return (
    <div className="w-full max-w-[1300px]">
      <div className="flex min-h-9 min-w-0 flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <h1 className="min-w-0 break-words text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
          {materialImportText(locale, "reviewTitle")}
        </h1>
        <Link to={`/tasks/${taskId ?? ""}/questions`} className="inline-flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4" />
          {materialImportText(locale, "backToOverview")}
        </Link>
      </div>
      <NewTaskStepper currentStep={2} />

      <section className="mt-[30px] min-w-0 overflow-hidden rounded-[10px] border bg-card">
        <div className="flex min-h-[64px] flex-col gap-2 border-b px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[13px] leading-5 text-muted-foreground">{materialImportText(locale, "reviewDescription")}</p>
          {plan ? (
            <p className="text-[12px] text-muted-foreground">
              {plan.source.filename} · {materialImportText(locale, "selectedCountPrefix")}{selectedCount}{materialImportText(locale, "selectedCountSuffix")}
            </p>
          ) : null}
        </div>

        {importQuery.isLoading || taskQuery.isLoading || plan?.status === "running" ? (
          <div className="flex min-h-[320px] items-center justify-center">
            <LoaderCircle className="h-6 w-6 animate-spin text-primary" />
          </div>
        ) : importQuery.isError || plan?.status === "error" ? (
          <div className="flex min-h-[320px] flex-col items-center justify-center px-5 text-center">
            <p className="text-sm font-semibold text-danger">{materialImportText(locale, "reviewLoadError")}</p>
            <p className="mt-2 max-w-xl text-xs leading-5 text-muted-foreground">
              {planExpired
                ? (locale === "zh-CN" ? "匹配计划已过期，请重新选择资料并生成候选。" : "The match plan expired. Choose the material and generate candidates again.")
                : (plan?.error ?? "")}
            </p>
            {planExpired ? (
              <Link
                to={`/tasks/${taskId ?? ""}/questions/import`}
                className="mt-4 inline-flex h-9 items-center rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground hover:opacity-90"
              >
                {locale === "zh-CN" ? "返回重新选择并匹配" : "Choose & Match Again"}
              </Link>
            ) : (
              <button type="button" onClick={() => void importQuery.refetch()} className="mt-4 inline-flex h-9 items-center gap-2 rounded-[7px] border bg-card px-4 text-sm font-semibold hover:bg-muted">
                <RefreshCw className="h-4 w-4" />{materialImportText(locale, "refresh")}
              </button>
            )}
          </div>
        ) : taskQuery.isError || !taskQuery.isSuccess ? (
          <div className="flex min-h-[320px] flex-col items-center justify-center px-5 text-center">
            <p className="text-sm font-semibold text-danger">{locale === "zh-CN" ? "无法读取当前题目资料" : "Current question materials could not be loaded"}</p>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{locale === "zh-CN" ? "重新读取后才能安全确认是否覆盖现有内容。" : "Reload the task before deciding whether any existing content may be overwritten."}</p>
            <button type="button" onClick={() => void taskQuery.refetch()} className="mt-4 inline-flex h-9 items-center gap-2 rounded-[7px] border bg-card px-4 text-sm font-semibold hover:bg-muted">
              <RefreshCw className="h-4 w-4" />{materialImportText(locale, "refresh")}
            </button>
          </div>
        ) : sortedCandidates.length === 0 ? (
          <div className="p-5">
            <EmptyState
              title={materialImportText(locale, "reviewEmpty")}
              action={<Link className="text-sm font-semibold text-primary hover:underline" to={`/tasks/${taskId ?? ""}/questions`}>{materialImportText(locale, "backToOverview")}</Link>}
            />
          </div>
        ) : (
          <CandidateMatrix
            candidates={sortedCandidates}
            problems={problems}
            acceptedIds={acceptedIds}
            overwriteIds={overwriteIds}
            disabled={Boolean(invalidPlanReason) || applyImport.isPending}
            locale={locale}
            onToggleCandidate={toggleCandidate}
            onToggleOverwrite={toggleOverwrite}
          />
        )}

        {plan?.status === "ready" && sortedCandidates.length > 0 && taskQuery.isSuccess ? (
          <footer className="flex min-h-[66px] flex-col gap-3 border-t px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground">
                {materialImportText(locale, "selectedCountPrefix")}{selectedCount}{materialImportText(locale, "selectedCountSuffix")}
              </p>
              {invalidPlanReason ? <p role="alert" className="mt-1 text-xs text-danger">{invalidPlanReason}</p> : null}
              {applyError ? <p role="alert" className="mt-1 text-xs text-danger">{applyError}</p> : null}
            </div>
            {invalidPlanReason ? (
              <Link
                to={`/tasks/${taskId ?? ""}/questions/import`}
                onClick={() => { allowLeaveRef.current = true; }}
                className="inline-flex h-10 w-full items-center justify-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground hover:opacity-90 sm:w-[220px]"
              >
                {locale === "zh-CN" ? "返回重新选择并匹配" : "Choose & Match Again"}
              </Link>
            ) : (
              <button
                type="button"
                disabled={selectedCount === 0 || applyImport.isPending}
                onClick={() => void handleApply()}
                className="inline-flex h-10 w-full items-center justify-center rounded-[8px] bg-primary px-5 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 sm:w-[220px]"
              >
                {applyImport.isPending ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : null}
                {materialImportText(locale, applyImport.isPending ? "applying" : "apply")}
              </button>
            )}
          </footer>
        ) : null}
      </section>

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

function CandidateMatrix({ candidates, problems, acceptedIds, overwriteIds, disabled, locale, onToggleCandidate, onToggleOverwrite }: {
  candidates: MaterialImportCandidate[];
  problems: Record<string, ProblemInfo>;
  acceptedIds: string[];
  overwriteIds: string[];
  disabled: boolean;
  locale: "zh-CN" | "en-US";
  onToggleCandidate: (candidate: MaterialImportCandidate, checked: boolean) => void;
  onToggleOverwrite: (candidate: MaterialImportCandidate, checked: boolean) => void;
}) {
  return (
    <div className="max-h-[calc(100vh-330px)] min-h-[260px] w-full min-w-0 overflow-auto overscroll-contain">
      <table className="w-full min-w-[1100px] border-collapse text-left text-[12px]">
        <thead className="sticky top-0 z-10 bg-muted/95 font-semibold text-muted-foreground backdrop-blur-sm">
          <tr className="border-b">
            <th className="w-[90px] px-5 py-3">{materialImportText(locale, "reviewQuestion")}</th>
            <th className="w-[145px] px-3 py-3">{materialImportText(locale, "reviewTarget")}</th>
            <th className="w-[130px] px-3 py-3">{materialImportText(locale, "reviewMatch")}</th>
            <th className="min-w-[270px] px-3 py-3">{materialImportText(locale, "reviewPreview")}</th>
            <th className="min-w-[230px] px-3 py-3">{materialImportText(locale, "reviewExisting")}</th>
            <th className="w-[170px] px-5 py-3">{materialImportText(locale, "reviewAction")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {candidates.map((candidate) => {
            const problem = problems[candidate.q_id];
            const conflict = candidate.would_overwrite;
            const possible = !conflict && candidate.match_status === "possible";
            const accepted = acceptedIds.includes(candidate.candidate_id);
            const overwrite = overwriteIds.includes(candidate.candidate_id);
            return (
              <tr key={candidate.candidate_id} className="align-top hover:bg-muted/20">
                <td className="px-5 py-3 font-semibold text-foreground">{problem?.number || candidate.q_id}</td>
                <td className="px-3 py-3 text-foreground">{targetLabel(candidate.target, locale)}</td>
                <td className="px-3 py-3">
                  <span className={cn("inline-flex items-center gap-1.5 font-semibold", conflict ? "text-danger" : possible ? "text-warning" : "text-accent")}>
                    <span className="h-1.5 w-1.5 rounded-full bg-current" />
                    {materialImportText(locale, conflict ? "conflict" : possible ? "possible" : "exact")}
                  </span>
                  <span className="mt-1 block text-[11px] text-muted-foreground">{Math.round(candidate.confidence * 100)}%</span>
                </td>
                <td className="px-3 py-3">
                  <p className="line-clamp-3 whitespace-pre-wrap leading-5 text-foreground">{candidateValue(candidate, locale)}</p>
                  {candidate.source_location || candidate.source_excerpt ? (
                    <p className="mt-1 line-clamp-1 text-[11px] text-muted-foreground" title={`${candidate.source_location} ${candidate.source_excerpt}`.trim()}>
                      {[candidate.source_location, candidate.source_excerpt].filter(Boolean).join(" · ")}
                    </p>
                  ) : null}
                </td>
                <td className="px-3 py-3"><p className="line-clamp-3 whitespace-pre-wrap leading-5 text-muted-foreground">{existingValue(problem, candidate.target, locale)}</p></td>
                <td className="px-5 py-3">
                  {conflict ? (
                    <label className="inline-flex cursor-pointer items-start gap-2 text-[12px] font-semibold text-danger">
                      <input type="checkbox" disabled={disabled} className="mt-0.5 h-4 w-4 accent-primary disabled:cursor-not-allowed disabled:opacity-50" checked={overwrite} onChange={(event) => onToggleOverwrite(candidate, event.target.checked)} />
                      <span>{overwrite ? materialImportText(locale, "overwrite") : materialImportText(locale, "noOverwrite")}</span>
                    </label>
                  ) : (
                    <label className="inline-flex cursor-pointer items-center gap-2 text-[12px] font-semibold text-foreground">
                      <input type="checkbox" disabled={disabled} className="h-4 w-4 accent-primary disabled:cursor-not-allowed disabled:opacity-50" checked={accepted} onChange={(event) => onToggleCandidate(candidate, event.target.checked)} />
                      {materialImportText(locale, "selected")}
                    </label>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function targetLabel(target: MaterialImportTarget, locale: "zh-CN" | "en-US") {
  if (target === "criterion") return materialImportText(locale, "targetRubric");
  if (target === "reference_answer") return materialImportText(locale, "targetAnswer");
  return materialImportText(locale, "targetTests");
}

function targetOrder(target: MaterialImportTarget) {
  return target === "criterion" ? 0 : target === "reference_answer" ? 1 : 2;
}

function candidateValue(candidate: MaterialImportCandidate, locale: "zh-CN" | "en-US") {
  if (candidate.target !== "test_cases") return candidate.text_value?.trim() || "—";
  return (candidate.test_cases ?? []).slice(0, 3).map((testCase, index) => (
    locale === "zh-CN"
      ? `样例 ${index + 1}：${testCase.input || "—"} → ${testCase.expected_output || "—"}`
      : `Case ${index + 1}: ${testCase.input || "—"} → ${testCase.expected_output || "—"}`
  )).join("\n") || "—";
}

function existingValue(problem: ProblemInfo | undefined, target: MaterialImportTarget, locale: "zh-CN" | "en-US") {
  if (!problem) return "—";
  if (target === "criterion") return problem.criterion?.trim() || (locale === "zh-CN" ? "无" : "None");
  if (target === "reference_answer") return problem.reference_answer?.trim() || (locale === "zh-CN" ? "无" : "None");
  const cases = problem.test_cases ?? [];
  return cases.length
    ? (locale === "zh-CN" ? `${cases.length} 个测试样例` : `${cases.length} test ${cases.length === 1 ? "case" : "cases"}`)
    : (locale === "zh-CN" ? "无" : "None");
}

function sameIdSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((id) => rightSet.has(id));
}

import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  CheckCircle2,
  FileDown,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  Users,
} from "lucide-react";
import { lazy, Suspense, useEffect, type ComponentType, type ReactNode } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";
import { useTask, useTaskFinalization, useTaskResult } from "@/api/hooks/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import {
  buildResultsModel,
  clampPercent,
  formatPercent,
  summarizeReviewScoreSources,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { formatTaskTime, getTaskDestination } from "@/lib/taskFlow";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { QuestionAnalysisDetail } from "@/routes/tasks/results/QuestionAnalysisDetail";
import { QuestionAnalysisOverview } from "@/routes/tasks/results/QuestionAnalysisOverview";
import { ResultsSummaryMetric } from "@/routes/tasks/results/ResultsSummaryMetric";
import { StudentAnalysisDetail } from "@/routes/tasks/results/StudentAnalysisDetail";
import { StudentAnalysisOverview } from "@/routes/tasks/results/StudentAnalysisOverview";
import type { TaskFinalizationResponse, TaskResultResponse } from "@/types";

const VisualizationAnalysisPage = lazy(() => import("@/routes/tasks/results/VisualizationAnalysisPage").then((module) => ({ default: module.VisualizationAnalysisPage })));
const ReportsDownloadsPage = lazy(() => import("@/routes/tasks/results/ReportsDownloadsPage").then((module) => ({ default: module.ReportsDownloadsPage })));

type WorkspaceSection = "overview" | "questions" | "students" | "visualizations" | "reports";

interface WorkspaceNavItem {
  key: WorkspaceSection;
  label: string;
  labelEn: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  suffix: string;
}

const WORKSPACE_NAV: WorkspaceNavItem[] = [
  { key: "overview", label: "总览", labelEn: "Overview", icon: LayoutDashboard, suffix: "" },
  { key: "questions", label: "题目分析", labelEn: "Question analysis", icon: ListChecks, suffix: "/questions" },
  { key: "students", label: "学生分析", labelEn: "Student analysis", icon: Users, suffix: "/students" },
  { key: "visualizations", label: "可视化分析", labelEn: "Visual analysis", icon: BarChart3, suffix: "/visualizations" },
  { key: "reports", label: "报告与下载", labelEn: "Reports & downloads", icon: FileDown, suffix: "/reports" },
];

const RESULT_WORKSPACE_STATUSES = new Set(["graded", "review_confirmed", "generating_analysis", "finalized"]);
const OVERVIEW_CHART_PALETTE = ["#f08f9b", "#f3b780", "#5ec7ae", "#7c8cf8", "#a995e8"];
type OverviewPanelTone = "primary" | "accent" | "secondary" | "warning";

const OVERVIEW_PANEL_STYLES: Record<OverviewPanelTone, string> = {
  primary: "border-border/75 bg-white",
  accent: "border-border/75 bg-white",
  secondary: "border-border/75 bg-white",
  warning: "border-border/75 bg-white",
};

const OVERVIEW_PANEL_TITLE_STYLES: Record<OverviewPanelTone, string> = {
  primary: "text-[#4f61c9]",
  accent: "text-[#247d69]",
  secondary: "text-[#7058b2]",
  warning: "text-[#a56524]",
};

/** A-00: Figma-16 visual language, expanded into the confirmed five-route workspace. */
export function FinalResultsWorkspacePage() {
  const { taskId, questionId, studentId } = useParams();
  const { locale } = useI18n();
  const location = useLocation();
  const taskQuery = useTask(taskId, { refetchOnMount: "always" });
  const task = taskQuery.data;
  const resultWorkspaceReady = Boolean(task && RESULT_WORKSPACE_STATUSES.has(task.status));
  const resultQuery = useTaskResult(taskId, { enabled: resultWorkspaceReady });
  const finalizationQuery = useTaskFinalization(taskId, { enabled: resultWorkspaceReady });
  const section = sectionFromPath(location.pathname);
  const provisional = task?.status === "graded";
  const waitingForFreshTaskStatus = Boolean(
    task && taskQuery.isFetching && !RESULT_WORKSPACE_STATUSES.has(task.status),
  );

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [questionId, section, studentId]);

  if (!taskId) return <Navigate replace to="/history" />;
  if (taskQuery.isLoading || waitingForFreshTaskStatus) {
    return <WorkspaceState locale={locale} loading />;
  }
  if (task?.status === "grading") return <Navigate replace to={`/tasks/${taskId}/grading/progress`} />;
  if (taskId && task && !RESULT_WORKSPACE_STATUSES.has(task.status)) {
    return <Navigate replace to={getTaskDestination(task)} />;
  }
  if (taskId && provisional && section === "reports") {
    return <Navigate replace to={`/tasks/${taskId}/results`} />;
  }

  if (taskQuery.isLoading || resultQuery.isLoading || finalizationQuery.isLoading) {
    return <WorkspaceState locale={locale} loading />;
  }
  if (taskQuery.isError || resultQuery.isError || finalizationQuery.isError || !task || !finalizationQuery.data) {
    return (
      <WorkspaceState
        locale={locale}
        error={taskQuery.error ?? resultQuery.error ?? finalizationQuery.error ?? "formal_result_unavailable"}
        returnTo={`/tasks/${taskId}/results`}
        onRetry={() => void Promise.all([taskQuery.refetch(), resultQuery.refetch(), finalizationQuery.refetch()])}
      />
    );
  }

  const root = `/tasks/${encodeURIComponent(taskId)}/results`;
  const finalization = finalizationQuery.data;
  const result = resultQuery.data;

  return (
    <div className="w-full max-w-[1300px]">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
            {tx(locale, "学情分析与导出", "Performance Analysis & Exports")}
          </h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {task.name} · {provisional
              ? tx(locale, `未确认结果 · 工作流 r${finalization.workflow_revision}`, `Unconfirmed results · workflow r${finalization.workflow_revision}`)
              : tx(locale, `正式结果 v${finalization.final_result_version}`, `Final Results v${finalization.final_result_version}`)}
          </p>
        </div>
        <Link to={`/tasks/${encodeURIComponent(taskId)}/review`} className="text-[13px] font-semibold text-primary hover:underline">
          {tx(locale, "返回复核记录", "Review History")}
        </Link>
      </div>

      <NewTaskStepper currentStep={7} />

      <ResultStateBanner locale={locale} taskId={taskId} finalization={finalization} provisional={provisional} />

      <WorkspaceNavigation locale={locale} root={root} section={section} provisional={provisional} />

      <main className="mt-5 min-w-0">
        <WorkspaceContent
          locale={locale}
          section={section}
          taskId={taskId}
          taskName={task.name}
          questionId={questionId}
          studentId={studentId}
          finalization={finalization}
          result={result}
          provisional={provisional}
        />
      </main>
    </div>
  );
}

function WorkspaceNavigation({ locale, root, section, provisional }: { locale: Locale; root: string; section: WorkspaceSection; provisional: boolean }) {
  const items = provisional ? WORKSPACE_NAV.filter((item) => item.key !== "reports") : WORKSPACE_NAV;
  return (
    <nav
      className={cn(
        "mt-5 flex snap-x gap-2 overflow-x-auto rounded-[10px] border bg-card p-2 overscroll-x-contain lg:grid lg:overflow-visible",
        provisional ? "lg:grid-cols-4" : "lg:grid-cols-5",
      )}
      aria-label={tx(locale, "结果工作区", "Results workspace")}
    >
      {items.map((item) => {
        const Icon = item.icon;
        const active = item.key === section;
        return (
          <Link
            key={item.key}
            to={`${root}${item.suffix}`}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-[70px] min-w-[188px] snap-start items-center gap-3 rounded-[8px] border px-4 py-3 outline-none transition lg:min-w-0",
              "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
              active
                ? "border-blue-200 bg-blue-50/80 text-primary shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-blue-900 dark:bg-blue-950/30"
                : "border-transparent text-muted-foreground hover:border-border hover:bg-muted/60 hover:text-foreground",
            )}
          >
            <span className={cn(
              "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px]",
              active ? "bg-blue-100 text-primary dark:bg-blue-900/50" : "bg-muted text-muted-foreground",
            )}>
              <Icon aria-hidden={true} className="h-[18px] w-[18px]" />
            </span>
            <span className="min-w-0">
              <span className="block truncate text-[14px] font-bold leading-5">{item.label}</span>
              <span className="mt-0.5 block truncate text-[10px] font-medium opacity-70">{item.labelEn}</span>
            </span>
          </Link>
        );
      })}
    </nav>
  );
}

function ResultStateBanner({ locale, taskId, finalization, provisional }: { locale: Locale; taskId: string; finalization: TaskFinalizationResponse; provisional: boolean }) {
  const stale = finalization.final_result_dirty || finalization.analysis_status === "stale";
  if (provisional) {
    return (
      <section className="mt-5 flex min-h-16 flex-wrap items-center gap-4 rounded-[10px] border border-amber-200 bg-amber-50 px-5 py-3">
        <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-100 text-amber-700">
          <AlertTriangle aria-hidden="true" className="h-[18px] w-[18px]" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-[14px] font-bold text-foreground">
            {tx(locale, "批改已完成，可预览当前统计", "Grading is complete; current analysis is available")}
          </p>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            {tx(locale, "这些是尚未确认的批改结果；报告、下载与正式发布仍需教师完成复核。", "These grading results are unconfirmed. Reports, downloads, and release remain locked until teacher review is complete.")}
          </p>
        </div>
        <Link
          to={`/tasks/${encodeURIComponent(taskId)}/review`}
          className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-[8px] border border-amber-300 bg-card px-4 text-[12px] font-semibold text-amber-800 outline-none hover:bg-amber-100 focus-visible:ring-2 focus-visible:ring-ring"
        >
          {tx(locale, "返回复核", "Return to review")}
          <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
        </Link>
      </section>
    );
  }
  return (
    <section className={cn(
      "mt-5 flex min-h-16 flex-wrap items-center gap-4 rounded-[10px] border px-5 py-3",
      stale ? "border-amber-200 bg-amber-50" : "border-blue-100 bg-blue-50/80",
    )}>
      <span className={cn("inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full", stale ? "bg-amber-100 text-amber-700" : "bg-blue-100 text-primary")}>
        {stale ? <AlertTriangle aria-hidden="true" className="h-[18px] w-[18px]" /> : <CheckCircle2 aria-hidden="true" className="h-[18px] w-[18px]" />}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-bold text-foreground">
          {stale
            ? tx(locale, "结果已修改，需要重新确认正式版本", "Results changed; confirm the final version again")
            : tx(locale, `正式结果 v${finalization.final_result_version} 已保存`, `Final Results v${finalization.final_result_version} saved`)}
        </p>
        <p className="mt-0.5 text-[12px] text-muted-foreground">
          {tx(locale, "确认时间", "Confirmed")}：{formatTaskTime(finalization.final_result_updated_at ?? undefined, true, locale)} · {analysisStatusLabel(locale, finalization.analysis_status)}
        </p>
      </div>
      <Link
        to={stale ? `/tasks/${encodeURIComponent(taskId)}/review` : `/tasks/${encodeURIComponent(taskId)}/results/reports`}
        className={cn(
          "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-[8px] px-4 text-[12px] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring",
          stale ? "border border-amber-300 bg-card text-amber-800 hover:bg-amber-100" : "bg-primary text-primary-foreground hover:opacity-90",
        )}
      >
        {stale ? tx(locale, "返回复核并重新确认", "Review and reconfirm") : tx(locale, "报告与下载", "Reports & downloads")}
        <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
      </Link>
    </section>
  );
}

function WorkspaceContent({
  locale,
  section,
  taskId,
  taskName,
  questionId,
  studentId,
  finalization,
  result,
  provisional,
}: {
  locale: Locale;
  section: WorkspaceSection;
  taskId: string;
  taskName: string;
  questionId?: string;
  studentId?: string;
  finalization: TaskFinalizationResponse;
  result?: TaskResultResponse;
  provisional: boolean;
}) {
  const model = buildResultsModel(undefined, result);

  if (section === "overview") {
    return <ResultsOverview locale={locale} taskId={taskId} finalization={finalization} model={model} provisional={provisional} />;
  }

  if (section === "questions") {
    return questionId
      ? <QuestionAnalysisDetail locale={locale} taskId={taskId} questionId={questionId} model={model} />
      : <QuestionAnalysisOverview locale={locale} taskId={taskId} model={model} />;
  }

  if (section === "students") {
    return studentId
      ? <StudentAnalysisDetail locale={locale} taskId={taskId} studentId={studentId} model={model} />
      : <StudentAnalysisOverview locale={locale} taskId={taskId} model={model} />;
  }

  if (section === "visualizations") {
    return <Suspense fallback={<section className="flex min-h-64 items-center justify-center rounded-[10px] border bg-card"><LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" /></section>}><VisualizationAnalysisPage locale={locale} taskId={taskId} version={finalization.final_result_version} model={model} provisional={provisional} /></Suspense>;
  }
  return <Suspense fallback={<section className="flex min-h-64 items-center justify-center rounded-[10px] border bg-card"><LoaderCircle aria-hidden="true" className="h-7 w-7 animate-spin text-primary" /></section>}><ReportsDownloadsPage locale={locale} taskId={taskId} taskName={taskName} finalization={finalization} /></Suspense>;
}

function ResultsOverview({
  locale,
  taskId,
  finalization,
  model,
  provisional,
}: {
  locale: Locale;
  taskId: string;
  finalization: TaskFinalizationResponse;
  model: ResultsModel;
  provisional: boolean;
}) {
  const root = `/tasks/${encodeURIComponent(taskId)}/results`;
  const scoreDistribution = buildScoreDistribution(model.students);
  const strongestBucket = Math.max(1, ...scoreDistribution.map((bucket) => bucket.count));
  const weakQuestions = [...model.questions]
    .filter((question) => question.avgPercent !== null)
    .sort((left, right) => (left.avgPercent ?? 0) - (right.avgPercent ?? 0))
    .slice(0, 3);
  const studentPreview = [...model.students]
    .sort((left, right) => (left.percent ?? Number.POSITIVE_INFINITY) - (right.percent ?? Number.POSITIVE_INFINITY))
    .slice(0, 3);
  const validStudentPercents = model.students
    .map((student) => student.percent)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const passCount = validStudentPercents.filter((value) => value >= 60).length;
  const passRate = validStudentPercents.length ? (passCount / validStudentPercents.length) * 100 : null;
  const reviewSources = summarizeReviewScoreSources(model.students.flatMap((student) => student.corrections));
  const reviewConclusion = reviewSources.total
    ? tx(
        locale,
        `${reviewSources.teacherConfirmedSame + reviewSources.teacherChanged} 项教师已处理 · ${reviewSources.aiUntouched} 项沿用有效 AI 分${reviewSources.hardFailure ? ` · ${reviewSources.hardFailure} 项无有效分数` : ""}`,
        `${reviewSources.teacherConfirmedSame + reviewSources.teacherChanged} teacher-handled · ${reviewSources.aiUntouched} using valid AI scores${reviewSources.hardFailure ? ` · ${reviewSources.hardFailure} unscored` : ""}`,
      )
    : tx(locale, "无复核信号", "No review signals");

  return (
    <section className="relative overflow-hidden rounded-[10px] border border-t-2 border-t-primary/65 bg-card p-5">
      <div className="relative">
        <SectionHeading
          title={tx(locale, "结果总览", "Results overview")}
          description={tx(
            locale,
            provisional ? "当前批改结果的班级摘要；教师确认前仅供分析与复核。" : "正式结果的简洁班级摘要；详细信息分别进入独立分析页面。",
            provisional ? "A class summary of the current grading results, for analysis and review before teacher confirmation." : "A concise class summary of the final results, with dedicated pages for details.",
          )}
        />

      <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
        <ResultsSummaryMetric value={String(model.students.length)} label={tx(locale, "学生数", "Students")} tone="primary" size="large" />
        <ResultsSummaryMetric value={String(model.questions.length)} label={tx(locale, "题目数", "Questions")} tone="accent" size="large" />
        <ResultsSummaryMetric value={formatPercent(model.classAveragePercent)} label={tx(locale, "班级平均得分率", "Class Average")} tone="warning" size="large" />
        <ResultsSummaryMetric value={formatPercent(passRate)} label={tx(locale, "及格率（≥60%）", "Pass rate (≥60%)")} tone="secondary" size="large" />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <OverviewPanel
          title={tx(locale, "分数分布", "Score distribution")}
          subtitle={tx(locale, "按学生总得分率分桶", "Students grouped by overall score percentage")}
          href={`${root}/visualizations`}
          linkLabel={tx(locale, "查看可视化", "View visualizations")}
          tone="primary"
        >
          {validStudentPercents.length ? (
            <div className="flex h-[118px] items-end justify-between gap-3 pt-3" aria-label={tx(locale, "学生分数分布", "Student score distribution")}>
              {scoreDistribution.map((bucket, index) => (
                <div key={bucket.label} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-1.5">
                  <span className="text-[11px] font-semibold text-muted-foreground">{bucket.count}</span>
                  <span
                    className="w-full max-w-10 rounded-t-[6px]"
                    style={{
                      backgroundColor: OVERVIEW_CHART_PALETTE[index % OVERVIEW_CHART_PALETTE.length],
                      height: `${Math.max(5, (bucket.count / strongestBucket) * 76)}px`,
                    }}
                    title={`${bucket.label}: ${bucket.count}`}
                  />
                  <span className="whitespace-nowrap text-[10px] text-muted-foreground">{bucket.label}</span>
                </div>
              ))}
            </div>
          ) : <CompactEmpty locale={locale} />}
        </OverviewPanel>

        <OverviewPanel
          title={tx(locale, "薄弱题目预览", "Lowest-scoring questions")}
          subtitle={tx(locale, "按平均得分率从低到高", "Ordered by average score percentage")}
          href={`${root}/questions`}
          linkLabel={tx(locale, "查看题目分析", "View questions")}
          tone="warning"
        >
          {weakQuestions.length ? (
            <div className="mt-3 grid gap-3">
              {weakQuestions.map((question) => (
                <QuestionPreviewRow key={question.id} locale={locale} question={question} />
              ))}
            </div>
          ) : <CompactEmpty locale={locale} />}
        </OverviewPanel>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <OverviewPanel
          title={tx(locale, "学生表现预览", "Student performance preview")}
          subtitle={tx(locale, "低得分率优先，仅显示 3 位", "Lowest score percentages first; 3 students shown")}
          href={`${root}/students`}
          linkLabel={tx(locale, "查看学生分析", "View students")}
          tone="accent"
        >
          {studentPreview.length ? (
            <div className="mt-2 divide-y">
              {studentPreview.map((student) => <StudentPreviewRow key={student.id} locale={locale} student={student} />)}
            </div>
          ) : <CompactEmpty locale={locale} />}
        </OverviewPanel>

        <OverviewPanel
          title={tx(locale, "复核与产物", "Review & Downloads")}
          subtitle={reviewConclusion}
          href={provisional ? `/tasks/${encodeURIComponent(taskId)}/review` : `${root}/reports`}
          linkLabel={provisional ? tx(locale, "完成教师复核", "Complete teacher review") : tx(locale, "查看报告状态", "View report status")}
          tone="secondary"
        >
          <div className="mt-3 grid gap-2 text-[12px]">
            <StatusLine
              label={tx(locale, "正式结果", "Final Results")}
              value={provisional ? tx(locale, "尚未确认", "Unconfirmed") : `v${finalization.final_result_version}`}
              tone="primary"
            />
            <StatusLine
              label={tx(locale, "分析状态", "Analysis status")}
              value={provisional ? tx(locale, "即时预览可用", "Live preview available") : analysisStatusLabel(locale, finalization.analysis_status)}
              tone={finalization.analysis_status === "stale" ? "warning" : "neutral"}
            />
            <StatusLine
              label={tx(locale, "报告下载", "Report downloads")}
              value={provisional ? tx(locale, "复核确认后开放", "Available after confirmation") : finalization.analysis_status === "ready" && finalization.analysis_result_version === finalization.final_result_version ? tx(locale, `v${finalization.final_result_version} 可下载`, `v${finalization.final_result_version} ready`) : tx(locale, "尚未生成，进入报告页查看", "Not generated; see report page")}
              tone={finalization.analysis_status === "ready" ? "primary" : "neutral"}
            />
          </div>
        </OverviewPanel>
      </div>
        </div>
    </section>
  );
}

function OverviewPanel({
  title,
  subtitle,
  href,
  linkLabel,
  tone,
  children,
}: {
  title: string;
  subtitle: string;
  href: string;
  linkLabel: string;
  tone: OverviewPanelTone;
  children: ReactNode;
}) {
  return (
    <section className={cn("min-w-0 rounded-[9px] border px-4 py-3.5 shadow-[0_12px_30px_-28px_rgba(40,56,99,0.65)]", OVERVIEW_PANEL_STYLES[tone])}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className={cn("text-[14px] font-bold", OVERVIEW_PANEL_TITLE_STYLES[tone])}>{title}</h3>
          <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{subtitle}</p>
        </div>
        <Link to={href} className="inline-flex shrink-0 items-center gap-1 text-[11px] font-semibold text-primary hover:underline">
          {linkLabel}
          <ArrowRight aria-hidden="true" className="h-3 w-3" />
        </Link>
      </div>
      {children}
    </section>
  );
}

function QuestionPreviewRow({ locale, question }: { locale: Locale; question: QuestionSummary }) {
  const percent = clampPercent(question.avgPercent);
  const barColor = percent < 60 ? "#f08f9b" : percent < 75 ? "#f3b780" : "#5ec7ae";
  return (
    <div className="grid grid-cols-[minmax(72px,0.8fr)_minmax(120px,1.4fr)_38px] items-center gap-3">
      <span className="truncate text-[12px] font-semibold text-foreground">{question.label}</span>
      <span className="h-2 overflow-hidden rounded-full bg-muted">
        <span className="block h-full rounded-full" style={{ backgroundColor: barColor, width: `${percent}%` }} />
      </span>
      <span className="text-right text-[11px] font-semibold text-muted-foreground">{formatPercent(question.avgPercent)}</span>
      <span className="col-span-3 -mt-2 truncate text-[10px] text-muted-foreground">
        {question.stem || tx(locale, "暂无题干摘要", "No stem preview")}
      </span>
    </div>
  );
}

function StudentPreviewRow({ locale, student }: { locale: Locale; student: StudentSummary }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 py-2 first:pt-1 last:pb-0">
      <span className="truncate text-[12px] font-semibold text-foreground">{student.name} · {student.id}</span>
      <span className="text-[11px] text-muted-foreground">
        {student.lowConfidenceCount ? tx(locale, `${student.lowConfidenceCount} 个低置信题次`, `${student.lowConfidenceCount} low-confidence`) : tx(locale, "无低置信题次", "No low-confidence items")}
      </span>
      <span className="min-w-10 text-right text-[12px] font-bold text-[#4f61c9]">{formatPercent(student.percent)}</span>
    </div>
  );
}

function StatusLine({ label, value, tone }: { label: string; value: string; tone: "primary" | "warning" | "neutral" }) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-[7px] border border-border/70 bg-white px-3 py-2">
      <span className="font-medium text-muted-foreground">{label}</span>
      <span className={cn("text-right font-semibold", tone === "primary" && "text-primary", tone === "warning" && "text-amber-600", tone === "neutral" && "text-foreground")}>{value}</span>
    </div>
  );
}

function CompactEmpty({ locale }: { locale: Locale }) {
  return <p className="flex min-h-[82px] items-center justify-center text-[12px] text-muted-foreground">{tx(locale, "当前版本没有可统计的数据。", "No statistical data is available in this version.")}</p>;
}

function buildScoreDistribution(students: StudentSummary[]) {
  const buckets = [
    { label: "<60", min: Number.NEGATIVE_INFINITY, max: 60, count: 0 },
    { label: "60–69", min: 60, max: 70, count: 0 },
    { label: "70–79", min: 70, max: 80, count: 0 },
    { label: "80–89", min: 80, max: 90, count: 0 },
    { label: "90–100", min: 90, max: Number.POSITIVE_INFINITY, count: 0 },
  ];
  for (const student of students) {
    if (student.percent === null || !Number.isFinite(student.percent)) continue;
    const bucket = buckets.find((candidate) => student.percent! >= candidate.min && student.percent! < candidate.max);
    if (bucket) bucket.count += 1;
  }
  return buckets;
}

function SectionHeading({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">{title}</h2>
      <p className="mt-1 text-[13px] text-muted-foreground">{description}</p>
    </div>
  );
}

function WorkspaceState({
  locale,
  loading = false,
  error,
  returnTo,
  onRetry,
}: {
  locale: Locale;
  loading?: boolean;
  error?: unknown;
  returnTo?: string;
  onRetry?: () => void;
}) {
  if (!loading && error) {
    const info = classifyRecoverableError(error, { locale, phase: "formal_results", returnTo });
    return (
      <RecoverableActionState
        info={info}
        locale={locale}
        className="min-h-[420px]"
        primaryAction={info.actionKind === "byok" ? undefined : onRetry ? {
          label: tx(locale, "重新读取", "Try again"),
          onClick: onRetry,
        } : undefined}
        secondaryAction={{ label: tx(locale, "返回历史任务", "View task history"), href: "/history" }}
      />
    );
  }
  return (
    <div className="flex min-h-[420px] flex-col items-center justify-center rounded-[10px] border bg-card px-6 text-center">
      <LoaderCircle aria-hidden="true" className="h-8 w-8 animate-spin text-primary" />
      <h1 className="mt-4 text-lg font-bold text-foreground">{loading ? tx(locale, "正在读取正式结果…", "Loading final results…") : tx(locale, "正式结果暂时无法读取", "Final results are unavailable")}</h1>
    </div>
  );
}

function sectionFromPath(pathname: string): WorkspaceSection {
  if (/\/results\/questions(?:\/[^/]+)?\/?$/.test(pathname)) return "questions";
  if (/\/results\/students(?:\/[^/]+)?\/?$/.test(pathname)) return "students";
  if (pathname.endsWith("/visualizations")) return "visualizations";
  if (pathname.endsWith("/reports")) return "reports";
  return "overview";
}

function analysisStatusLabel(locale: Locale, status: TaskFinalizationResponse["analysis_status"]): string {
  const labels = {
    not_generated: ["分析尚未生成", "Analysis not generated"],
    generating: ["分析生成中", "Analysis generating"],
    ready: ["分析已生成", "Analysis ready"],
    stale: ["分析已过期", "Analysis stale"],
  } as const;
  return labels[status][locale === "en-US" ? 1 : 0];
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

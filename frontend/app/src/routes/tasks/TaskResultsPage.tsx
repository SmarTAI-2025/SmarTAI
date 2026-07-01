import { type FormEvent, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  CheckCircle2,
  FileText,
  Filter,
  Loader2,
  Search,
  Sparkles,
  RefreshCw,
  ShieldAlert,
  UserRound,
  UsersRound,
  XCircle,
} from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useAnalyticsQuery } from "@/api/hooks/analytics";
import { useTask, useTaskResult } from "@/api/hooks/tasks";
import {
  buildResultsModel,
  clampPercent,
  formatConfidence,
  formatPercent,
  formatScore,
  hasReviewSignal,
  ResultsLayout,
  reviewReasonLabel,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/ResultsLayout";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Textarea } from "@/components/ui/Input";
import { StatTile } from "@/components/ui/StatTile";
import { cn } from "@/lib/cn";
import type { ChartAnalyticsResult, ChartTrace, ChartTraceType, Correction, TaskStatus } from "@/types";

type PlotDatum = {
  type: ChartTraceType;
  x?: Array<string | number>;
  y?: Array<string | number>;
  labels?: string[];
  values?: number[];
  name?: string;
};

const ALLOWED_CHART_TRACE_TYPES = new Set<ChartTraceType>(["bar", "scatter", "pie", "histogram", "box"]);

const markdownComponents: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-6">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
  code: ({ children }) => <code className="rounded bg-muted px-1 py-0.5 text-xs">{children}</code>,
};

const STATUS_LABELS: Partial<Record<TaskStatus | "completed" | "not_found", string>> = {
  draft: "草稿",
  extracting_problems: "题目识别中",
  problems_ready: "题目已就绪",
  parsing_submissions: "作答解析中",
  submissions_ready: "作答已就绪",
  grading: "批改中",
  graded: "已完成",
  completed: "已完成",
  error: "出错",
  not_found: "结果记录不可用",
};

export function TaskResultsPage() {
  const { taskId } = useParams();
  const [searchParams] = useSearchParams();
  const view = searchParams.get("view");
  const context = view === "questions" ? "by-question" : view === "charts" ? "visualization" : "overview";
  const title =
    context === "by-question" ? "按题分析" : context === "visualization" ? "结果可视化" : "批改结果总览";
  const description =
    context === "by-question"
      ? "按题查看全班得分、复核信号与单题详情。"
      : context === "visualization"
        ? "用当前批改结果展示分数分布与按题平均表现。"
        : "总览班级表现、低置信题次与学生详情入口。";

  const taskQuery = useTask(taskId);
  const resultQuery = useTaskResult(taskId);
  const model = useMemo(() => buildResultsModel(taskQuery.data, resultQuery.data), [taskQuery.data, resultQuery.data]);
  const firstStudentId = model.students[0]?.id ?? null;
  const firstQuestionId = model.questions[0]?.id ?? null;

  if (!taskId) {
    return <EmptyState title="缺少任务 ID" description="请从教师工作台或任务列表进入结果页。" />;
  }

  return (
    <ResultsLayout
      context={context}
      title={title}
      description={description}
      detailTargets={{ studentId: firstStudentId, questionId: firstQuestionId }}
    >
      {taskQuery.isLoading || resultQuery.isLoading ? <LoadingCard /> : null}
      {taskQuery.isError || resultQuery.isError ? (
        <ErrorCard
          error={taskQuery.error ?? resultQuery.error}
          onRetry={() => {
            void taskQuery.refetch();
            void resultQuery.refetch();
          }}
        />
      ) : null}
      {!taskQuery.isLoading && !resultQuery.isLoading && !taskQuery.isError && !resultQuery.isError ? (
        <ResultsContent
          taskId={taskId}
          context={context}
          model={model}
          status={resultQuery.data?.status ?? taskQuery.data?.status}
        />
      ) : null}
    </ResultsLayout>
  );
}

function ResultsContent({
  taskId,
  context,
  model,
  status,
}: {
  taskId: string;
  context: "overview" | "by-question" | "visualization";
  model: ResultsModel;
  status?: TaskStatus | "completed" | "not_found";
}) {
  if (status !== "completed") {
    return (
      <EmptyState
        title="结果尚未生成"
        description={`当前任务状态：${STATUS_LABELS[status ?? "draft"] ?? status ?? "未知"}。批改完成后这里会显示真实结果。`}
        action={
          <Link to={`/tasks/${taskId}/upload/submissions`}>
            <Button type="button" variant="secondary">
              返回批改流程
            </Button>
          </Link>
        }
      />
    );
  }

  if (!model.students.length) {
    return (
      <EmptyState
        title="暂无批改结果"
        description="后端返回了完成状态，但没有学生结果数据。可以刷新页面或返回上传页检查作答。"
        action={
          <Link to={`/tasks/${taskId}/upload/submissions`}>
            <Button type="button" variant="secondary">
              检查学生作答
            </Button>
          </Link>
        }
      />
    );
  }

  if (context === "by-question") {
    return <ByQuestionView taskId={taskId} model={model} />;
  }

  if (context === "visualization") {
    return <VisualizationView taskId={taskId} model={model} />;
  }

  return <OverviewView taskId={taskId} model={model} />;
}

interface ActiveStudentFilter {
  prompt: string;
  explanation: string;
  studentIds: string[];
  unknownStudentIds: string[];
}

function OverviewView({ taskId, model }: { taskId: string; model: ResultsModel }) {
  const [activeFilter, setActiveFilter] = useState<ActiveStudentFilter | null>(null);
  const activeStudentIds = useMemo(
    () => (activeFilter ? new Set(activeFilter.studentIds) : null),
    [activeFilter],
  );
  const visibleStudents = useMemo(
    () => (activeStudentIds ? model.students.filter((student) => activeStudentIds.has(student.id)) : model.students),
    [activeStudentIds, model.students],
  );
  const reviewItems = collectReviewItems(model)
    .filter((item) => !activeStudentIds || activeStudentIds.has(item.student.id))
    .slice(0, 8);

  return (
    <div className="grid gap-4">
      <OverviewAnalyticsPanel
        taskId={taskId}
        students={model.students}
        activeFilter={activeFilter}
        onFilterChange={setActiveFilter}
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile icon={UsersRound} label="已批改学生" value={model.students.length} tone="primary" />
        <StatTile
          icon={BarChart3}
          label="平均分"
          value={`${formatScore(model.classAverageScore)} / ${formatScore(model.classAverageMax)}`}
          tone="accent"
        />
        <StatTile icon={ShieldAlert} label="低置信题次" value={model.lowConfidenceCount} tone="warning" />
        <StatTile icon={CheckCircle2} label="需复核题次" value={model.reviewCount} tone="danger" />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <Card className="grid gap-4">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h2 className="text-base font-semibold">学生列表</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {activeFilter ? `已筛选 ${visibleStudents.length} / ${model.students.length} 人` : `班级平均得分率 ${formatPercent(model.classAveragePercent)}`}
                {model.timestamp ? `，结果生成于 ${formatTimestamp(model.timestamp)}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {activeFilter ? (
                <Button type="button" variant="ghost" onClick={() => setActiveFilter(null)}>
                  <XCircle className="h-4 w-4" />
                  清除筛选
                </Button>
              ) : null}
              <Link to={`/tasks/${taskId}/results?view=questions`}>
                <Button type="button" variant="secondary">
                  <FileText className="h-4 w-4" />
                  按题分析
                </Button>
              </Link>
            </div>
          </div>
          {visibleStudents.length ? (
            <StudentTable taskId={taskId} students={visibleStudents} />
          ) : (
            <EmptyState title="没有匹配学生" description="当前筛选没有命中学生。清除筛选后可查看全班结果。" />
          )}
        </Card>

        <Card className="grid content-start gap-4">
          <div>
            <h2 className="text-base font-semibold">复核队列</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              汇总低置信或后端标记需复核的题次，便于快速进入学生或题目详情。
            </p>
          </div>
          {reviewItems.length ? (
            <div className="grid gap-2">
              {reviewItems.map((item) => (
                <div key={`${item.student.id}-${item.correction.q_id}`} className="rounded-md border p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium">{item.student.name}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.question.label} · {formatScore(item.correction.score)} / {formatScore(item.correction.max_score)} ·
                        置信度 {formatConfidence(item.correction.confidence)}
                      </p>
                    </div>
                    <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
                  </div>
                  <ReviewReasons correction={item.correction} />
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Link to={`/tasks/${taskId}/results/${item.student.id}`}>
                      <Button type="button" variant="ghost" className="h-8 px-2">
                        学生详情
                      </Button>
                    </Link>
                    <Link to={`/tasks/${taskId}/questions/${item.question.id}`}>
                      <Button type="button" variant="ghost" className="h-8 px-2">
                        题目详情
                      </Button>
                    </Link>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed p-4 text-sm leading-6 text-muted-foreground">
              当前结果没有低置信或需复核题次。
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function OverviewAnalyticsPanel({
  taskId,
  students,
  activeFilter,
  onFilterChange,
}: {
  taskId: string;
  students: StudentSummary[];
  activeFilter: ActiveStudentFilter | null;
  onFilterChange: (filter: ActiveStudentFilter | null) => void;
}) {
  const [summaryPrompt, setSummaryPrompt] = useState("概括本次批改中最需要关注的 3 个班级表现。");
  const [filterPrompt, setFilterPrompt] = useState("找出需要优先人工复核的学生。");
  const summaryQuery = useAnalyticsQuery();
  const filterQuery = useAnalyticsQuery();
  const summaryResult = summaryQuery.data?.mode === "summary" ? summaryQuery.data : null;
  const matchedStudents = activeFilter ? students.filter((student) => activeFilter.studentIds.includes(student.id)) : [];

  const handleSummarySubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const question = summaryPrompt.trim();
    if (!question) {
      toast.error("请输入摘要请求。");
      return;
    }
    summaryQuery.mutate(
      { taskId, question, mode: "summary" },
      {
        onSuccess: (result) => {
          if (result.mode !== "summary") {
            toast.error("摘要返回格式不匹配。");
            return;
          }
          toast.success("摘要已生成。");
        },
        onError: (error) => {
          toast.error("摘要生成失败", { description: formatAnalyticsError(error) });
        },
      },
    );
  };

  const handleFilterSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const question = filterPrompt.trim();
    if (!question) {
      toast.error("请输入筛选请求。");
      return;
    }
    filterQuery.mutate(
      { taskId, question, mode: "filter" },
      {
        onSuccess: (result) => {
          if (result.mode !== "filter") {
            toast.error("筛选返回格式不匹配。");
            return;
          }
          const filter = buildActiveStudentFilter(question, result.explanation, result.student_ids, students);
          onFilterChange(filter);
          if (filter.studentIds.length) {
            toast.success(`已匹配 ${filter.studentIds.length} 名学生。`);
          } else {
            toast.info("筛选没有命中学生。");
          }
        },
        onError: (error) => {
          toast.error("筛选失败", { description: formatAnalyticsError(error) });
        },
      },
    );
  };

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card className="grid content-start gap-4">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Sparkles className="h-4 w-4 text-accent" />
            自然语言摘要
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">基于当前任务的真实批改结果生成。</p>
        </div>
        <form className="grid gap-3" onSubmit={handleSummarySubmit}>
          <Textarea
            value={summaryPrompt}
            onChange={(event) => setSummaryPrompt(event.target.value)}
            rows={3}
            disabled={summaryQuery.isPending}
          />
          <div className="flex justify-end">
            <Button type="submit" disabled={summaryQuery.isPending || !summaryPrompt.trim()}>
              {summaryQuery.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              生成摘要
            </Button>
          </div>
        </form>
        {summaryQuery.isPending ? <InlineStatus tone="muted" message="正在生成摘要..." /> : null}
        {summaryQuery.isError ? <InlineStatus tone="danger" message={formatAnalyticsError(summaryQuery.error)} /> : null}
        {summaryResult ? <MarkdownBlock markdown={summaryResult.markdown} /> : null}
      </Card>

      <Card className="grid content-start gap-4">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Filter className="h-4 w-4 text-primary" />
            自然语言筛选
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">筛选结果会同步限制下方学生列表。</p>
        </div>
        <form className="grid gap-3" onSubmit={handleFilterSubmit}>
          <Textarea
            value={filterPrompt}
            onChange={(event) => setFilterPrompt(event.target.value)}
            rows={3}
            disabled={filterQuery.isPending}
          />
          <div className="flex justify-end">
            <Button type="submit" disabled={filterQuery.isPending || !filterPrompt.trim()}>
              {filterQuery.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              筛选学生
            </Button>
          </div>
        </form>
        {filterQuery.isPending ? <InlineStatus tone="muted" message="正在筛选学生..." /> : null}
        {filterQuery.isError ? <InlineStatus tone="danger" message={formatAnalyticsError(filterQuery.error)} /> : null}
        {activeFilter ? (
          <div className="grid gap-3 rounded-md border bg-muted/30 p-3">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-sm font-medium">筛选解释</p>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  {activeFilter.explanation || "后端没有返回筛选解释。"}
                </p>
              </div>
              <Button type="button" variant="ghost" className="shrink-0" onClick={() => onFilterChange(null)}>
                <XCircle className="h-4 w-4" />
                清除
              </Button>
            </div>
            <div>
              <p className="text-sm font-medium">匹配学生</p>
              {matchedStudents.length ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {matchedStudents.map((student) => (
                    <Link
                      key={student.id}
                      to={`/tasks/${taskId}/results/${student.id}`}
                      className="rounded-full border bg-card px-2.5 py-1 text-xs font-medium transition hover:bg-muted"
                    >
                      {student.name}
                    </Link>
                  ))}
                </div>
              ) : (
                <p className="mt-1 text-sm text-muted-foreground">没有匹配到当前结果里的学生。</p>
              )}
              {activeFilter.unknownStudentIds.length ? (
                <p className="mt-2 text-xs text-warning">
                  {activeFilter.unknownStudentIds.length} 个后端返回的学生 ID 不在当前结果中。
                </p>
              ) : null}
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

function ByQuestionView({ taskId, model }: { taskId: string; model: ResultsModel }) {
  if (!model.questions.length) {
    return <EmptyState title="暂无题目数据" description="当前结果中没有可统计的题目。" />;
  }

  return (
    <Card className="grid gap-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-base font-semibold">题目维度统计</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            根据题目数据与学生批改结果计算平均分、得分率和复核信号。
          </p>
        </div>
        <Link to={`/tasks/${taskId}/results?view=charts`}>
          <Button type="button" variant="secondary">
            <BarChart3 className="h-4 w-4" />
            查看图表
          </Button>
        </Link>
      </div>
      <div className="grid gap-3">
        {model.questions.map((question) => (
          <QuestionSummaryRow key={question.id} taskId={taskId} question={question} />
        ))}
      </div>
    </Card>
  );
}

function VisualizationView({ taskId, model }: { taskId: string; model: ResultsModel }) {
  return (
    <div className="grid gap-4">
      <ChartQueryPanel taskId={taskId} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="grid gap-4">
          <div>
            <h2 className="text-base font-semibold">分数分布</h2>
            <p className="mt-1 text-sm text-muted-foreground">按学生总得分率分桶统计。</p>
          </div>
          <ScoreDistribution students={model.students} />
        </Card>
        <Card className="grid gap-4">
          <div>
            <h2 className="text-base font-semibold">按题平均</h2>
            <p className="mt-1 text-sm text-muted-foreground">每题平均得分率，便于快速定位薄弱题目。</p>
          </div>
          <QuestionAverageChart questions={model.questions} />
        </Card>
      </div>
    </div>
  );
}

function ChartQueryPanel({ taskId }: { taskId: string }) {
  const [chartPrompt, setChartPrompt] = useState("画出学生总分率分布，并突出需要复核的情况。");
  const chartQuery = useAnalyticsQuery();
  const chartResult = chartQuery.data?.mode === "chart" ? chartQuery.data : null;
  const chartRender = useMemo(() => (chartResult ? buildPlotlyPayload(chartResult) : null), [chartResult]);

  const handleChartSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const question = chartPrompt.trim();
    if (!question) {
      toast.error("请输入图表请求。");
      return;
    }
    chartQuery.mutate(
      { taskId, question, mode: "chart" },
      {
        onSuccess: (result) => {
          if (result.mode !== "chart") {
            toast.error("图表返回格式不匹配。");
            return;
          }
          toast.success("图表已生成。");
        },
        onError: (error) => {
          toast.error("图表生成失败", { description: formatAnalyticsError(error) });
        },
      },
    );
  };

  return (
    <Card className="grid gap-4">
      <div>
        <h2 className="flex items-center gap-2 text-base font-semibold">
          <BarChart3 className="h-4 w-4 text-primary" />
          自然语言图表
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">后端只会返回当前图表白名单中的 trace 类型。</p>
      </div>
      <form className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start" onSubmit={handleChartSubmit}>
        <Textarea
          value={chartPrompt}
          onChange={(event) => setChartPrompt(event.target.value)}
          rows={3}
          disabled={chartQuery.isPending}
        />
        <Button type="submit" className="lg:mt-0" disabled={chartQuery.isPending || !chartPrompt.trim()}>
          {chartQuery.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <BarChart3 className="h-4 w-4" />}
          生成图表
        </Button>
      </form>
      {chartQuery.isPending ? <InlineStatus tone="muted" message="正在生成图表..." /> : null}
      {chartQuery.isError ? <InlineStatus tone="danger" message={formatAnalyticsError(chartQuery.error)} /> : null}
      {chartResult ? (
        <div className="grid gap-3 rounded-md border bg-muted/20 p-3">
          <div>
            <h3 className="text-sm font-semibold">{chartResult.title || "图表"}</h3>
            {chartResult.rationale ? (
              <p className="mt-1 text-sm leading-6 text-muted-foreground">{chartResult.rationale}</p>
            ) : null}
          </div>
          {chartRender?.data.length ? (
            <SafeChartPreview data={chartRender.data} title={chartRender.title} />
          ) : (
            <InlineStatus tone="warning" message="后端没有返回可渲染的图表数据。" />
          )}
          {chartRender && chartRender.omittedTraceCount > 0 ? (
            <InlineStatus tone="warning" message={`已忽略 ${chartRender.omittedTraceCount} 个不在白名单内的 trace。`} />
          ) : null}
        </div>
      ) : (
        <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">暂无自然语言图表结果。</div>
      )}
    </Card>
  );
}

function SafeChartPreview({ data, title }: { data: PlotDatum[]; title?: string }) {
  return (
    <div className="grid gap-4 rounded-md bg-background p-4">
      {title ? <p className="text-sm font-medium">{title}</p> : null}
      <div className="grid gap-4 xl:grid-cols-2">
        {data.map((trace, index) => (
          <TracePreview key={`${trace.name ?? trace.type}-${index}`} trace={trace} index={index} />
        ))}
      </div>
    </div>
  );
}

function TracePreview({ trace, index }: { trace: PlotDatum; index: number }) {
  const title = trace.name || `${trace.type} #${index + 1}`;

  if (trace.type === "pie") {
    const labels = trace.labels ?? [];
    const values = trace.values ?? [];
    const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
    return (
      <div className="grid gap-3 rounded-md border p-3">
        <p className="text-sm font-semibold">{title}</p>
        {labels.map((label, itemIndex) => {
          const value = Math.max(0, values[itemIndex] ?? 0);
          const percent = total > 0 ? (value / total) * 100 : 0;
          return (
            <ChartBarRow
              key={`${label}-${itemIndex}`}
              label={label}
              value={`${formatScore(value)} (${formatPercent(percent)})`}
              percent={percent}
              tone={itemIndex % 2 === 0 ? "accent" : "primary"}
            />
          );
        })}
      </div>
    );
  }

  const labels = buildTraceLabels(trace);
  const values = buildTraceValues(trace);

  if (!values.length) {
    return (
      <div className="rounded-md border p-3 text-sm text-muted-foreground">
        {title} 没有可预览的数据点。
      </div>
    );
  }

  if (trace.type === "scatter") {
    return <ScatterPreview title={title} labels={labels} values={values} />;
  }

  const maxValue = Math.max(1, ...values.map((value) => Math.abs(value)));
  return (
    <div className="grid gap-3 rounded-md border p-3">
      <p className="text-sm font-semibold">{title}</p>
      {values.slice(0, 20).map((value, itemIndex) => (
        <ChartBarRow
          key={`${labels[itemIndex] ?? itemIndex}-${itemIndex}`}
          label={labels[itemIndex] ?? String(itemIndex + 1)}
          value={formatScore(value)}
          percent={(Math.abs(value) / maxValue) * 100}
          tone={trace.type === "box" ? "warning" : trace.type === "histogram" ? "primary" : "accent"}
        />
      ))}
      {values.length > 20 ? (
        <p className="text-xs text-muted-foreground">仅预览前 20 个数据点。</p>
      ) : null}
    </div>
  );
}

function ScatterPreview({ title, labels, values }: { title: string; labels: string[]; values: number[] }) {
  const maxValue = Math.max(1, ...values.map((value) => Math.abs(value)));
  return (
    <div className="grid gap-3 rounded-md border p-3">
      <p className="text-sm font-semibold">{title}</p>
      <div className="flex h-44 items-end gap-1 rounded-md bg-muted/40 p-3">
        {values.slice(0, 24).map((value, index) => (
          <div key={`${labels[index] ?? index}-${index}`} className="flex flex-1 flex-col items-center gap-1">
            <div
              className="w-full rounded-t bg-primary"
              style={{ height: `${Math.max(4, (Math.abs(value) / maxValue) * 100)}%` }}
              title={`${labels[index] ?? index + 1}: ${formatScore(value)}`}
            />
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">散点结果以柱状缩略预览，详细交互图表留待浏览器验收后增强。</p>
    </div>
  );
}

function ChartBarRow({
  label,
  value,
  percent,
  tone,
}: {
  label: string;
  value: string;
  percent: number;
  tone: "accent" | "primary" | "warning";
}) {
  const color = tone === "warning" ? "bg-warning" : tone === "primary" ? "bg-primary" : "bg-accent";
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)_72px] items-center gap-3 text-sm">
      <span className="truncate text-muted-foreground" title={label}>
        {label}
      </span>
      <div className="h-6 overflow-hidden rounded-md bg-muted">
        <div className={cn("h-full rounded-md", color)} style={{ width: `${clampPercent(percent)}%` }} />
      </div>
      <span className="text-right tabular-nums">{value}</span>
    </div>
  );
}

function StudentTable({ taskId, students }: { taskId: string; students: StudentSummary[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="border-b text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">学生</th>
            <th className="px-3 py-2 font-medium">总分</th>
            <th className="px-3 py-2 font-medium">得分率</th>
            <th className="px-3 py-2 font-medium">平均置信度</th>
            <th className="px-3 py-2 font-medium">复核信号</th>
            <th className="px-3 py-2 text-right font-medium">详情</th>
          </tr>
        </thead>
        <tbody>
          {students.map((student) => (
            <tr key={student.id} className="border-b last:border-0">
              <td className="px-3 py-3">
                <div className="font-medium">{student.name}</div>
                <div className="text-xs text-muted-foreground">{student.id}</div>
              </td>
              <td className="px-3 py-3">
                {formatScore(student.totalScore)} / {formatScore(student.totalMax)}
              </td>
              <td className="px-3 py-3">
                <div className="flex min-w-36 items-center gap-2">
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                    <div className="h-full bg-accent" style={{ width: `${clampPercent(student.percent)}%` }} />
                  </div>
                  <span className="w-10 text-right tabular-nums">{formatPercent(student.percent)}</span>
                </div>
              </td>
              <td className="px-3 py-3">{formatConfidence(student.avgConfidence)}</td>
              <td className="px-3 py-3">
                {student.lowConfidenceCount || student.reviewCount ? (
                  <span className="rounded-full bg-warning/10 px-2 py-1 text-xs font-medium text-warning">
                    低置信 {student.lowConfidenceCount} · 需复核 {student.reviewCount}
                  </span>
                ) : (
                  <span className="text-muted-foreground">无</span>
                )}
              </td>
              <td className="px-3 py-3 text-right">
                <Link to={`/tasks/${taskId}/results/${student.id}`} className="inline-flex">
                  <Button type="button" variant="ghost" className="h-8 px-2">
                    <UserRound className="h-4 w-4" />
                    查看
                  </Button>
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QuestionSummaryRow({ taskId, question }: { taskId: string; question: QuestionSummary }) {
  return (
    <Link
      to={`/tasks/${taskId}/questions/${question.id}`}
      className="grid gap-3 rounded-lg border p-3 transition hover:bg-muted/60 lg:grid-cols-[minmax(0,1fr)_260px_auto]"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{question.label}</span>
          {question.type ? <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{question.type}</span> : null}
          {question.reviewCount || question.lowConfidenceCount ? (
            <span className="rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
              低置信 {question.lowConfidenceCount} · 需复核 {question.reviewCount}
            </span>
          ) : null}
        </div>
        {question.stem ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{question.stem}</p> : null}
      </div>
      <div className="grid content-center gap-1 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">平均分</span>
          <span className="font-medium">
            {formatScore(question.avgScore)} / {formatScore(question.maxScore)}
          </span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary" style={{ width: `${clampPercent(question.avgPercent)}%` }} />
        </div>
        <div className="text-right text-xs text-muted-foreground">{formatPercent(question.avgPercent)}</div>
      </div>
      <div className="flex items-center justify-end gap-1 text-sm font-medium text-primary">
        详情
        <ArrowRight className="h-4 w-4" />
      </div>
    </Link>
  );
}

function ScoreDistribution({ students }: { students: StudentSummary[] }) {
  const buckets = [
    { label: "0-59%", min: 0, max: 60 },
    { label: "60-69%", min: 60, max: 70 },
    { label: "70-79%", min: 70, max: 80 },
    { label: "80-89%", min: 80, max: 90 },
    { label: "90-100%", min: 90, max: 101 },
  ].map((bucket) => ({
    ...bucket,
    count: students.filter((student) => {
      const percent = student.percent ?? 0;
      return percent >= bucket.min && percent < bucket.max;
    }).length,
  }));
  const maxCount = Math.max(1, ...buckets.map((bucket) => bucket.count));

  return (
    <div className="grid gap-3">
      {buckets.map((bucket) => (
        <div key={bucket.label} className="grid grid-cols-[72px_minmax(0,1fr)_42px] items-center gap-3 text-sm">
          <span className="text-muted-foreground">{bucket.label}</span>
          <div className="h-7 overflow-hidden rounded-md bg-muted">
            <div
              className={cn("h-full rounded-md bg-accent transition-all", bucket.count === 0 ? "opacity-30" : "")}
              style={{ width: `${(bucket.count / maxCount) * 100}%` }}
            />
          </div>
          <span className="text-right tabular-nums">{bucket.count}</span>
        </div>
      ))}
    </div>
  );
}

function QuestionAverageChart({ questions }: { questions: QuestionSummary[] }) {
  if (!questions.length) {
    return <EmptyState title="暂无题目数据" description="当前结果中没有可视化的题目统计。" />;
  }

  return (
    <div className="grid gap-3">
      {questions.map((question) => (
        <div key={question.id} className="grid grid-cols-[72px_minmax(0,1fr)_56px] items-center gap-3 text-sm">
          <span className="truncate text-muted-foreground">{question.label}</span>
          <div className="h-7 overflow-hidden rounded-md bg-muted">
            <div className="h-full rounded-md bg-primary transition-all" style={{ width: `${clampPercent(question.avgPercent)}%` }} />
          </div>
          <span className="text-right tabular-nums">{formatPercent(question.avgPercent)}</span>
        </div>
      ))}
    </div>
  );
}

function ReviewReasons({ correction }: { correction: Correction }) {
  const reasons = correction.review_reasons ?? [];
  if (!reasons.length && !hasReviewSignal(correction)) {
    return null;
  }

  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {correction.confidence < 0.65 ? (
        <span className="rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">置信度偏低</span>
      ) : null}
      {reasons.map((reason) => (
        <span key={reason} className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {reviewReasonLabel(reason)}
        </span>
      ))}
    </div>
  );
}

function LoadingCard() {
  return (
    <Card className="flex items-center gap-3 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      正在读取批改结果...
    </Card>
  );
}

function ErrorCard({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <Card className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 text-danger" />
        <div>
          <p className="font-medium">结果读取失败</p>
          <p className="mt-1 text-sm text-muted-foreground">{getErrorMessage(error)}</p>
        </div>
      </div>
      <Button type="button" variant="secondary" onClick={onRetry}>
        <RefreshCw className="h-4 w-4" />
        重试
      </Button>
    </Card>
  );
}

function MarkdownBlock({ markdown }: { markdown: string }) {
  const content = markdown.trim();
  if (!content) {
    return <InlineStatus tone="warning" message="后端没有返回摘要内容。" />;
  }

  return (
    <div className="rounded-md border bg-muted/30 p-3 text-sm leading-6">
      <ReactMarkdown components={markdownComponents}>{content}</ReactMarkdown>
    </div>
  );
}

function InlineStatus({ tone, message }: { tone: "muted" | "warning" | "danger"; message: string }) {
  const toneClass =
    tone === "danger"
      ? "border-danger/30 bg-danger/5 text-danger"
      : tone === "warning"
        ? "border-warning/30 bg-warning/5 text-warning"
        : "border-border bg-muted/40 text-muted-foreground";
  return (
    <div className={cn("flex items-start gap-2 rounded-md border px-3 py-2 text-sm", toneClass)}>
      {tone === "muted" ? <Loader2 className="mt-0.5 h-4 w-4 animate-spin" /> : <AlertTriangle className="mt-0.5 h-4 w-4" />}
      <span className="leading-5">{message}</span>
    </div>
  );
}

function buildActiveStudentFilter(
  prompt: string,
  explanation: string,
  rawStudentIds: unknown,
  students: StudentSummary[],
): ActiveStudentFilter {
  const knownIds = new Set(students.map((student) => student.id));
  const uniqueIds = Array.from(
    new Set(
      Array.isArray(rawStudentIds)
        ? rawStudentIds
            .map((studentId) => (typeof studentId === "string" || typeof studentId === "number" ? String(studentId).trim() : ""))
            .filter(Boolean)
        : [],
    ),
  );
  return {
    prompt,
    explanation,
    studentIds: uniqueIds.filter((studentId) => knownIds.has(studentId)),
    unknownStudentIds: uniqueIds.filter((studentId) => !knownIds.has(studentId)),
  };
}

function buildPlotlyPayload(result: ChartAnalyticsResult): {
  data: PlotDatum[];
  title?: string;
  omittedTraceCount: number;
} {
  const data = (result.traces ?? []).map(toPlotDatum).filter((trace): trace is PlotDatum => Boolean(trace));

  return {
    data,
    title: result.layout?.title ?? result.title,
    omittedTraceCount: Math.max(0, (result.traces?.length ?? 0) - data.length),
  };
}

function buildTraceLabels(trace: PlotDatum): string[] {
  if (trace.labels?.length) {
    return trace.labels;
  }
  if (trace.x?.length) {
    return trace.x.map((value) => String(value));
  }
  if (trace.y?.length) {
    return trace.y.map((_, index) => String(index + 1));
  }
  return [];
}

function buildTraceValues(trace: PlotDatum): number[] {
  if (trace.values?.length) {
    return trace.values;
  }
  if (trace.y?.length) {
    return trace.y.map(toFiniteNumber).filter((value): value is number => value !== null);
  }
  if (trace.x?.length && (trace.type === "histogram" || trace.type === "box")) {
    return trace.x.map(toFiniteNumber).filter((value): value is number => value !== null);
  }
  return [];
}

function toFiniteNumber(value: string | number): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toPlotDatum(trace: ChartTrace): PlotDatum | null {
  const traceType = (trace as { type?: unknown }).type;
  if (!isAllowedChartTraceType(traceType)) {
    return null;
  }

  const datum: PlotDatum = { type: traceType };
  if (typeof trace.name === "string" && trace.name.trim()) {
    datum.name = trace.name;
  }

  if (traceType === "pie") {
    const labels = toStringArray((trace as { labels?: unknown }).labels);
    const values = toNumberArray((trace as { values?: unknown }).values);
    if (!labels?.length || !values?.length) {
      return null;
    }
    datum.labels = labels;
    datum.values = values;
    return datum;
  }

  const x = toStringOrNumberArray((trace as { x?: unknown }).x);
  const y = toStringOrNumberArray((trace as { y?: unknown }).y);
  if (!x?.length && !y?.length) {
    return null;
  }
  if (x?.length) {
    datum.x = x;
  }
  if (y?.length) {
    datum.y = y;
  }
  return datum;
}

function isAllowedChartTraceType(value: unknown): value is ChartTraceType {
  return typeof value === "string" && ALLOWED_CHART_TRACE_TYPES.has(value as ChartTraceType);
}

function toStringOrNumberArray(value: unknown) {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const items = value.filter(
    (item): item is string | number =>
      typeof item === "string" || (typeof item === "number" && Number.isFinite(item)),
  );
  return items.length ? items : undefined;
}

function toStringArray(value: unknown) {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const items = value
    .map((item) => (typeof item === "string" || typeof item === "number" ? String(item) : ""))
    .filter(Boolean);
  return items.length ? items : undefined;
}

function toNumberArray(value: unknown) {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const items = value.filter((item): item is number => typeof item === "number" && Number.isFinite(item));
  return items.length ? items : undefined;
}

function formatAnalyticsError(error: unknown) {
  const normalized = normalizeAPIError(error);
  const message = normalized.message || "";
  const lowerMessage = message.toLowerCase();

  if (normalized.status === 429) {
    return "分析请求过于频繁，请稍等片刻再试。";
  }
  if (
    normalized.status === 404 ||
    lowerMessage.includes("not graded") ||
    lowerMessage.includes("not completed") ||
    lowerMessage.includes("no result") ||
    message.includes("未批改")
  ) {
    return "当前任务还没有可分析的批改结果，请先完成批改。";
  }
  if (normalized.status >= 500) {
    return "后端分析服务暂时不可用，请稍后重试。";
  }
  return message || "分析请求失败，请稍后重试。";
}

function collectReviewItems(model: ResultsModel) {
  return model.students.flatMap((student) =>
    student.corrections
      .filter(hasReviewSignal)
      .map((correction) => ({
        student,
        correction,
        question: model.questions.find((question) => question.id === correction.q_id) ?? {
          id: correction.q_id,
          label: correction.q_id,
        },
      })),
  );
}

function formatTimestamp(timestamp: number) {
  const value = timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
  return new Date(value).toLocaleString("zh-CN");
}

function getErrorMessage(error: unknown) {
  if (error instanceof Error) {
    return error.message;
  }
  return "请稍后重试。";
}

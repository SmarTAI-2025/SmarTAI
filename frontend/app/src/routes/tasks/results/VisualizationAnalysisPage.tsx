import { BarChart3, Download, LoaderCircle, Printer, Save, Sparkles, Trash2 } from "lucide-react";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { toast } from "sonner";
import { useAnalyticsQuery } from "@/api/hooks/analytics";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import {
  effectiveCorrectionScore,
  formatPercent,
  formatScore,
  type QuestionSummary,
  type ResultsModel,
  type StudentSummary,
} from "@/components/tasks/resultsModel";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import type { ChartAnalyticsResult, ChartTrace, Correction } from "@/types";

type ScopeFilter = "all" | "pass" | "fail" | "review";

interface SavedChart {
  id: string;
  prompt: string;
  result: ChartAnalyticsResult;
}

const COLORS = {
  primary: "#2563eb",
  teal: "#14b8a6",
  amber: "#f59e0b",
  rose: "#f43f5e",
  slate: "#94a3b8",
  grid: "#e2e8f0",
};
const PIE_COLORS = [COLORS.teal, COLORS.rose, COLORS.slate];

export function VisualizationAnalysisPage({ locale, taskId, version, model }: { locale: Locale; taskId: string; version: number; model: ResultsModel }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const scope = normalizeScope(searchParams.get("scope"));
  const students = useMemo(() => model.students.filter((student) => matchesScope(student, scope)), [model.students, scope]);
  const studentIds = useMemo(() => new Set(students.map((student) => student.id)), [students]);
  const validPercents = students.map((student) => student.percent).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const mean = averageOrNull(validPercents);
  const median = medianOrNull(validPercents);
  const lowest = validPercents.length ? Math.min(...validPercents) : null;
  const highest = validPercents.length ? Math.max(...validPercents) : null;
  const passCount = validPercents.filter((value) => value >= 60).length;
  const questionData = buildQuestionPerformance(model.questions, studentIds);
  const distributionData = buildScoreDistribution(students);
  const scatterData = buildConfidenceScatter(students);
  const pieData = buildPassComposition(students);
  const [prompt, setPrompt] = useState(tx(locale, "画出学生得分率与平均置信度的关系，并区分需要复核的学生。", "Plot score percentage against average confidence and distinguish students who need review."));
  const [preview, setPreview] = useState<ChartAnalyticsResult | null>(null);
  const [savedCharts, setSavedCharts] = useState<SavedChart[]>([]);
  const chartQuery = useAnalyticsQuery();
  const root = `/tasks/${encodeURIComponent(taskId)}/results`;

  const updateScope = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "all") next.delete("scope");
    else next.set("scope", value);
    setSearchParams(next, { replace: true });
  };

  const runChart = (question: string) => {
    if (!question.trim()) return;
    chartQuery.mutate({ taskId, question, mode: "chart" }, {
      onSuccess: (result) => {
        if (result.mode !== "chart") {
          toast.error(tx(locale, "图表返回格式不匹配", "Chart response format did not match"));
          return;
        }
        setPreview(result);
        toast.success(tx(locale, "图表已生成", "Chart generated"));
      },
    });
  };

  const submitChart = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    runChart(prompt.trim());
  };

  const updatePrompt = (value: string) => {
    chartQuery.reset();
    setPrompt(value);
  };

  const recoveryInfo = chartQuery.isError
    ? classifyRecoverableError(chartQuery.error, {
      locale,
      phase: "analytics_chart",
      returnTo: `${root}/visualizations`,
    })
    : null;

  const savePreview = () => {
    if (!preview) return;
    setSavedCharts((items) => [{ id: `chart-${Date.now()}`, prompt: prompt.trim(), result: preview }, ...items]);
    toast.success(tx(locale, "已保存到本次浏览", "Saved for this visit"));
  };

  return (
    <section className="rounded-[10px] border bg-card">
      <div className="px-5 pt-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h2 className="text-[20px] font-bold tracking-[-0.01em] text-foreground">{tx(locale, "可视化分析", "Visual analysis")}</h2><p className="mt-1 text-[13px] text-muted-foreground">{tx(locale, "先用 SmarTAI 自然语言生成你关心的图表，也可继续查看下方默认分析。", "Start with SmarTAI natural-language charts, or continue to the default analysis below.")}</p></div>
          <div className="flex items-center gap-2"><select value={scope} onChange={(event) => updateScope(event.target.value)} aria-label={tx(locale, "选择图表数据范围", "Select chart data scope")} className="h-9 rounded-[8px] border bg-background px-3 text-[11px] font-semibold text-foreground outline-none focus:border-primary"><option value="all">{tx(locale, "全部学生", "All students")}</option><option value="pass">{tx(locale, "仅及格", "Passed only")}</option><option value="fail">{tx(locale, "仅未及格", "Failed only")}</option><option value="review">{tx(locale, "含必审信号", "With review signals")}</option></select><button type="button" onClick={() => window.print()} className="inline-flex h-9 items-center gap-1.5 rounded-[8px] border bg-card px-3 text-[11px] font-semibold text-foreground hover:bg-muted"><Printer aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "打印 / 存为 PDF", "Print / save PDF")}</button></div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-6">
          <SummaryMetric label={tx(locale, "当前样本", "Current sample")} value={String(students.length)} tone="primary" />
          <SummaryMetric label={tx(locale, "平均得分率", "Mean score")} value={formatPercent(mean)} tone="accent" />
          <SummaryMetric label={tx(locale, "中位得分率", "Median score")} value={formatPercent(median)} tone="primary" />
          <SummaryMetric label={tx(locale, "最低 / 最高", "Lowest / highest")} value={`${formatPercent(lowest)} / ${formatPercent(highest)}`} tone="warning" />
          <SummaryMetric label={tx(locale, "及格率（≥60%）", "Pass rate (≥60%)")} value={formatPercent(validPercents.length ? (passCount / validPercents.length) * 100 : null)} tone="accent" />
          <SummaryMetric label={tx(locale, "含必审信号", "With review signals")} value={String(students.filter(studentNeedsReview).length)} tone="danger" />
        </div>
      </div>

      <div className="mt-4 border-t p-5">
        <section className="relative overflow-hidden rounded-[10px] border border-primary/25 bg-gradient-to-br from-blue-50/90 via-card to-card px-4 py-4 dark:from-blue-950/25">
          <div aria-hidden="true" className="pointer-events-none absolute -right-12 -top-16 h-40 w-40 rounded-full bg-primary/5 blur-2xl" />
          <div className="relative flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-3">
              <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[9px] bg-primary text-primary-foreground shadow-sm">
                <Sparkles aria-hidden="true" className="h-4 w-4" />
              </span>
              <div>
                <h3 className="text-[16px] font-bold tracking-[-0.01em] text-foreground">{tx(locale, "SmarTAI 自然语言生成更多图表", "Generate more charts with SmarTAI")}</h3>
                <p className="mt-1 max-w-4xl text-[11px] leading-5 text-muted-foreground">{tx(locale, "直接描述希望比较的对象、指标和图表形式。每次提交只调用当前模型一次；支持柱状图、散点图、饼图、直方图或箱线图，单次最多 4 组、每组 50 个点。", "Describe what to compare, which metrics matter, and the chart form. Each submission calls the current model once and supports bar, scatter, pie, histogram, or box charts, with up to 4 series and 50 points per series.")}</p>
              </div>
            </div>
            <span className="rounded-full border border-blue-200 bg-white/80 px-2.5 py-1 text-[10px] font-semibold text-primary dark:border-blue-900 dark:bg-blue-950/40">{tx(locale, "按需调用模型", "Uses a model on demand")}</span>
          </div>
          <div className="relative mt-3 flex flex-wrap gap-2">{[tx(locale, "比较各题得分率与低置信题次", "Compare question score percentages and low-confidence counts"), tx(locale, "画出总分率与平均置信度散点图", "Plot overall score percentage against average confidence"), tx(locale, "显示及格与未及格人数", "Show pass and fail counts")].map((suggestion) => <button key={suggestion} type="button" onClick={() => updatePrompt(suggestion)} className="rounded-full border border-transparent bg-white/80 px-2.5 py-1 text-[10px] font-medium text-muted-foreground shadow-sm hover:border-primary/20 hover:text-primary dark:bg-slate-900/60">{suggestion}</button>)}</div>
          <form onSubmit={submitChart} className="relative mt-3 grid gap-2 lg:grid-cols-[minmax(0,1fr)_auto]">
            <textarea value={prompt} onChange={(event) => updatePrompt(event.target.value)} rows={2} maxLength={500} disabled={chartQuery.isPending} aria-label={tx(locale, "SmarTAI 自然语言图表请求", "SmarTAI natural-language chart request")} className="min-h-20 resize-y rounded-[8px] border bg-background px-3 py-2 text-[12px] leading-5 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
            <button type="submit" disabled={chartQuery.isPending || !prompt.trim()} className="inline-flex h-10 items-center justify-center gap-1.5 rounded-[8px] bg-primary px-4 text-[11px] font-semibold text-primary-foreground disabled:opacity-50 lg:self-end">{chartQuery.isPending ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : <BarChart3 aria-hidden="true" className="h-4 w-4" />}{chartQuery.isPending ? tx(locale, "SmarTAI 生成中…", "SmarTAI is generating…") : tx(locale, "让 SmarTAI 生成", "Generate with SmarTAI")}</button>
          </form>
          {recoveryInfo ? (
            <RecoverableActionState
              info={recoveryInfo}
              locale={locale}
              compact
              className="relative mt-3"
              primaryAction={recoveryInfo.actionKind === "byok" ? undefined : {
                label: recoveryInfo.actionLabel,
                onClick: () => runChart(prompt.trim()),
                busy: chartQuery.isPending,
              }}
              secondaryAction={recoveryInfo.actionKind === "byok"
                ? { label: tx(locale, "关闭提示", "Dismiss"), onClick: () => chartQuery.reset() }
                : { label: tx(locale, "查看模型配置", "View model settings"), href: `/settings/byok?returnTo=${encodeURIComponent(`${root}/visualizations`)}` }}
            />
          ) : null}
          {preview ? <GeneratedResult locale={locale} id="generated-preview" result={preview} version={version} onSave={savePreview} /> : <div className="relative mt-3 rounded-[8px] border border-dashed border-primary/20 bg-white/50 px-4 py-5 text-center text-[11px] text-muted-foreground dark:bg-slate-950/15">{tx(locale, "尚未生成自定义图表；下方五张默认图表始终可用且不消耗模型额度。", "No custom chart has been generated; the five default charts below remain available without model usage.")}</div>}
        </section>

        {savedCharts.length ? <section className="mt-4"><div className="flex items-end justify-between gap-3"><div><h3 className="text-[15px] font-bold text-foreground">{tx(locale, "本次浏览已保存", "Saved for this visit")}</h3><p className="mt-1 text-[10px] text-muted-foreground">{tx(locale, "这些图表只保留到刷新或离开本页；需要长期保存时请下载 PNG 或报告。", "These charts last until you refresh or leave this page. Download a PNG or report to keep them.")}</p></div><span className="text-[10px] text-muted-foreground">{savedCharts.length}</span></div><div className="mt-3 grid gap-4 xl:grid-cols-2">{savedCharts.map((item) => <GeneratedResult key={item.id} locale={locale} id={item.id} result={item.result} version={version} prompt={item.prompt} onDelete={() => setSavedCharts((items) => items.filter((candidate) => candidate.id !== item.id))} />)}</div></section> : null}
      </div>

      <div className="mt-4 grid gap-4 border-t p-5 xl:grid-cols-2">
        <ChartCard locale={locale} id="score-distribution" title={tx(locale, "得分率分布", "Score-rate distribution")} description={tx(locale, "查看成绩集中区间与尾部学生，不把人数少的区间夸大。", "Locate score clusters and tails without exaggerating sparse buckets.")} metadata={chartMetadata(locale, students.length, version, scope)} detailHref={`${root}/students`}>
          <ResponsiveContainer width="100%" height={250}><BarChart data={distributionData} margin={{ top: 12, right: 12, left: -20, bottom: 4 }}><CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" vertical={false} /><XAxis dataKey="label" tick={{ fontSize: 10 }} /><YAxis allowDecimals={false} tick={{ fontSize: 10 }} /><Tooltip contentStyle={tooltipStyle} /><Bar dataKey="count" name={tx(locale, "学生数", "Students")} fill={COLORS.primary} radius={[6, 6, 0, 0]} /></BarChart></ResponsiveContainer>
        </ChartCard>

        <ChartCard locale={locale} id="pass-composition" title={tx(locale, "及格构成", "Pass/Fail Breakdown")} description={tx(locale, "只比较互斥的及格、未及格与无可比总分，不把复核状态混入同一饼图。", "Compares mutually exclusive passed, failed, and unscored groups; review status is shown separately.")} metadata={chartMetadata(locale, students.length, version, scope)} detailHref={`${root}/students`}>
          <ResponsiveContainer width="100%" height={250}><PieChart><Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="47%" innerRadius={46} outerRadius={82} paddingAngle={3} label={({ name, percent = 0 }) => `${name} ${Math.round(percent * 100)}%`}>{pieData.map((entry, index) => <Cell key={entry.name} fill={PIE_COLORS[index % PIE_COLORS.length]} />)}</Pie><Tooltip contentStyle={tooltipStyle} /><Legend wrapperStyle={{ fontSize: 11 }} /></PieChart></ResponsiveContainer>
        </ChartCard>

        <ChartCard locale={locale} id="question-performance" title={tx(locale, "逐题得分率与复核量", "Question Score Percentage & Review Volume")} description={tx(locale, "柱表示平均得分率，折线表示必审题次数，用于同时发现薄弱题与证据风险。", "Bars show the average score percentage; the line shows required-review volume, highlighting both difficult questions and evidence risks.")} metadata={chartMetadata(locale, students.length, version, scope)} detailHref={`${root}/questions`} wide>
          <div style={{ width: `${Math.max(620, questionData.length * 72)}px`, height: 280 }}><ResponsiveContainer width="100%" height="100%"><ComposedChart data={questionData} margin={{ top: 14, right: 18, left: -10, bottom: 4 }}><CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" vertical={false} /><XAxis dataKey="label" tick={{ fontSize: 10 }} /><YAxis yAxisId="score" domain={[0, 100]} tick={{ fontSize: 10 }} unit="%" /><YAxis yAxisId="review" orientation="right" allowDecimals={false} tick={{ fontSize: 10 }} /><Tooltip contentStyle={tooltipStyle} /><Legend wrapperStyle={{ fontSize: 11 }} /><Bar yAxisId="score" dataKey="scoreRate" name={tx(locale, "平均得分率", "Average Score Percentage")} fill={COLORS.teal} radius={[5, 5, 0, 0]} /><Line yAxisId="review" type="monotone" dataKey="reviewCount" name={tx(locale, "必审题次", "Review Items")} stroke={COLORS.rose} strokeWidth={2} dot={{ r: 3 }} /></ComposedChart></ResponsiveContainer></div>
        </ChartCard>

        <ChartCard locale={locale} id="confidence-scatter" title={tx(locale, "得分率 × 平均置信度", "Score Percentage × Average Confidence")} description={tx(locale, "每个点是一位学生；红色只表示含正式必审信号，便于识别高分低置信等异常组合。", "Each point represents a student. Red indicates a required-review signal, helping identify patterns such as a high score with low confidence.")} metadata={chartMetadata(locale, students.length, version, scope)} detailHref={`${root}/students`} wide>
          <ResponsiveContainer width="100%" height={280}><ScatterChart margin={{ top: 16, right: 18, left: -4, bottom: 4 }}><CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" /><XAxis type="number" dataKey="score" name={tx(locale, "得分率", "Score Percentage")} domain={[0, 100]} unit="%" tick={{ fontSize: 10 }} /><YAxis type="number" dataKey="confidence" name={tx(locale, "平均置信度", "Average Confidence")} domain={[0, 100]} unit="%" tick={{ fontSize: 10 }} /><Tooltip cursor={{ strokeDasharray: "3 3" }} contentStyle={tooltipStyle} /><Legend wrapperStyle={{ fontSize: 11 }} /><Scatter name={tx(locale, "无必审项", "No required review")} data={scatterData.filter((item) => !item.review)} fill={COLORS.primary} /><Scatter name={tx(locale, "含必审信号", "With review signals")} data={scatterData.filter((item) => item.review)} fill={COLORS.rose} /></ScatterChart></ResponsiveContainer>
        </ChartCard>

        <ChartCard locale={locale} id="score-heatmap" title={tx(locale, "学生 × 题目得分热力图", "Student × Question Score Heatmap")} description={tx(locale, "颜色表达逐格得分率，单元格保留真实百分比；适合寻找个人薄弱题和班级共性。", "Cell color represents score percentage while retaining the exact value, revealing individual and class-wide weaknesses.")} metadata={chartMetadata(locale, Math.min(students.length, 100), version, scope, students.length > 100 ? tx(locale, "最多显示前 100 位", "First 100 students") : "")} detailHref={`${root}/students`} wide>
          <ScoreHeatmap locale={locale} students={students.slice(0, 100)} questions={model.questions} />
        </ChartCard>
      </div>

    </section>
  );
}

function ChartCard({ locale, id, title, description, metadata, detailHref, wide = false, children }: { locale: Locale; id: string; title: string; description: string; metadata: string[]; detailHref: string; wide?: boolean; children: ReactNode }) {
  return <article className={cn("min-w-0 rounded-[9px] border px-4 py-4", wide && "xl:col-span-2")}><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h3 className="text-[14px] font-bold text-foreground">{title}</h3><p className="mt-1 max-w-3xl text-[10px] leading-4 text-muted-foreground">{description}</p></div><div className="flex items-center gap-2"><button type="button" onClick={() => void exportChartPng(id, title, locale)} className="inline-flex h-8 items-center gap-1 rounded-[7px] border px-2.5 text-[10px] font-semibold text-foreground hover:bg-muted"><Download aria-hidden="true" className="h-3.5 w-3.5" />PNG</button><Link to={detailHref} className="text-[10px] font-semibold text-primary hover:underline">{tx(locale, "查看明细", "View details")}</Link></div></div><div className="mt-2 flex flex-wrap gap-1.5">{metadata.map((item) => <span key={item} className="rounded-full bg-slate-100 px-2 py-0.5 text-[9px] font-medium text-muted-foreground">{item}</span>)}</div><div data-chart-export={id} className={cn("mt-3 min-w-0", wide && "max-w-full overflow-x-auto")}>{children}</div></article>;
}

function GeneratedResult({ locale, id, result, version, prompt, onSave, onDelete }: { locale: Locale; id: string; result: ChartAnalyticsResult; version: number; prompt?: string; onSave?: () => void; onDelete?: () => void }) {
  const traces = result.traces.filter(isAllowedTrace).slice(0, 4);
  return <article className="mt-3 rounded-[9px] border px-4 py-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h4 className="text-[13px] font-bold text-foreground">{result.title || tx(locale, "自定义图表", "Custom chart")}</h4><p className="mt-1 text-[10px] leading-4 text-muted-foreground">{result.rationale || prompt}</p></div><div className="flex items-center gap-2"><button type="button" onClick={() => void exportChartPng(id, result.title, locale)} className="inline-flex h-8 items-center gap-1 rounded-[7px] border px-2 text-[10px] font-semibold"><Download aria-hidden="true" className="h-3.5 w-3.5" />{traces.length > 1 ? tx(locale, "首图 PNG", "Download First Chart") : "PNG"}</button>{onSave ? <button type="button" onClick={onSave} className="inline-flex h-8 items-center gap-1 rounded-[7px] bg-primary px-2.5 text-[10px] font-semibold text-primary-foreground"><Save aria-hidden="true" className="h-3.5 w-3.5" />{tx(locale, "保存本次", "Save")}</button> : null}{onDelete ? <button type="button" onClick={onDelete} aria-label={tx(locale, "删除本次保存图表", "Delete saved chart")} className="inline-flex h-8 w-8 items-center justify-center rounded-[7px] border text-rose-600 hover:bg-rose-50"><Trash2 aria-hidden="true" className="h-3.5 w-3.5" /></button> : null}</div></div><div className="mt-2 flex flex-wrap gap-1.5"><span className="rounded-full bg-slate-100 px-2 py-0.5 text-[9px] text-muted-foreground">v{version}</span><span className="rounded-full bg-slate-100 px-2 py-0.5 text-[9px] text-muted-foreground">{traces.length} series</span></div><div data-chart-export={id} className="mt-3 grid gap-3 xl:grid-cols-2">{traces.length ? traces.map((trace, index) => <GeneratedTrace key={`${trace.type}-${index}`} locale={locale} trace={trace} index={index} />) : <p className="col-span-full py-6 text-center text-[11px] text-muted-foreground">{tx(locale, "没有可安全渲染的 trace。", "No supported chart data to display.")}</p>}</div></article>;
}

function GeneratedTrace({ locale, trace, index }: { locale: Locale; trace: ChartTrace; index: number }) {
  const name = trace.name || `${trace.type} ${index + 1}`;
  if (trace.type === "pie") {
    const data = (trace.labels ?? []).slice(0, 50).map((label, itemIndex) => ({ name: label, value: numeric(trace.values?.[itemIndex]) ?? 0 }));
    return <TraceFrame title={name}><ResponsiveContainer width="100%" height={230}><PieChart><Pie data={data} dataKey="value" nameKey="name" innerRadius={38} outerRadius={72}>{data.map((item, itemIndex) => <Cell key={`${item.name}-${itemIndex}`} fill={[COLORS.primary, COLORS.teal, COLORS.amber, COLORS.rose, COLORS.slate][itemIndex % 5]} />)}</Pie><Tooltip contentStyle={tooltipStyle} /><Legend wrapperStyle={{ fontSize: 10 }} /></PieChart></ResponsiveContainer></TraceFrame>;
  }
  if (trace.type === "scatter") {
    const points = tracePoints(trace);
    return <TraceFrame title={name}><ResponsiveContainer width="100%" height={230}><ScatterChart margin={{ top: 12, right: 12, left: -8, bottom: 2 }}><CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" /><XAxis type="number" dataKey="x" tick={{ fontSize: 9 }} /><YAxis type="number" dataKey="y" tick={{ fontSize: 9 }} /><Tooltip contentStyle={tooltipStyle} /><Scatter data={points} fill={COLORS.primary} /></ScatterChart></ResponsiveContainer></TraceFrame>;
  }
  if (trace.type === "box") {
    return <TraceFrame title={name}><BoxPlotSvg values={traceValues(trace)} label={name} /></TraceFrame>;
  }
  const data = traceSeries(trace);
  return <TraceFrame title={name}><ResponsiveContainer width="100%" height={230}><BarChart data={data} margin={{ top: 12, right: 10, left: -16, bottom: 2 }}><CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" vertical={false} /><XAxis dataKey="label" tick={{ fontSize: 8 }} interval="preserveStartEnd" /><YAxis tick={{ fontSize: 9 }} /><Tooltip contentStyle={tooltipStyle} /><Bar dataKey="value" name={name} fill={trace.type === "histogram" ? COLORS.amber : COLORS.teal} radius={[4, 4, 0, 0]} /></BarChart></ResponsiveContainer></TraceFrame>;
}

function TraceFrame({ title, children }: { title: string; children: ReactNode }) { return <section className="min-w-0 rounded-[8px] bg-muted/35 px-3 py-3"><h5 className="truncate text-[10px] font-semibold text-foreground">{title}</h5><div className="mt-2">{children}</div></section>; }

function ScoreHeatmap({ locale, students, questions }: { locale: Locale; students: StudentSummary[]; questions: QuestionSummary[] }) {
  const cellWidth = 58;
  const left = 118;
  const header = 34;
  const rowHeight = 32;
  const width = Math.max(620, left + questions.length * cellWidth + 8);
  const height = Math.max(100, header + students.length * rowHeight + 8);
  return <div className="max-h-[420px] max-w-full overflow-auto"><svg role="img" aria-label={tx(locale, "学生题目得分热力图", "Student question score heatmap")} viewBox={`0 0 ${width} ${height}`} width={width} height={height} xmlns="http://www.w3.org/2000/svg"><rect width={width} height={height} fill="#ffffff" />{questions.map((question, qIndex) => <text key={question.id} x={left + qIndex * cellWidth + cellWidth / 2} y={21} textAnchor="middle" fontSize="10" fontWeight="600" fill="#64748b">{question.label}</text>)}{students.map((student, sIndex) => { const y = header + sIndex * rowHeight; return <g key={student.id}><text x={4} y={y + 20} fontSize="10" fill="#334155">{compactLabel(student.name, 16)}</text>{questions.map((question, qIndex) => { const correction = student.corrections.find((item) => item.q_id === question.id); const percent = correction && correction.max_score > 0 ? (effectiveCorrectionScore(correction) / correction.max_score) * 100 : null; const x = left + qIndex * cellWidth; return <g key={question.id}><rect x={x + 2} y={y + 3} width={cellWidth - 5} height={rowHeight - 6} rx="5" fill={heatColor(percent)} /><text x={x + cellWidth / 2} y={y + 20} textAnchor="middle" fontSize="9" fontWeight="600" fill={percent === null ? "#64748b" : percent < 60 ? "#be123c" : "#334155"}>{formatPercent(percent)}</text></g>; })}</g>; })}</svg></div>;
}

function BoxPlotSvg({ values, label }: { values: number[]; label: string }) {
  if (!values.length) return <p className="flex h-[210px] items-center justify-center text-[11px] text-muted-foreground">No numeric values</p>;
  const sorted = [...values].sort((a, b) => a - b);
  const min = sorted[0]; const max = sorted[sorted.length - 1]; const q1 = quantile(sorted, 0.25); const median = quantile(sorted, 0.5); const q3 = quantile(sorted, 0.75); const span = max - min || 1; const scale = (value: number) => 40 + ((value - min) / span) * 300;
  return <svg role="img" aria-label={`Box plot ${label}`} viewBox="0 0 380 210" width="100%" height="210" xmlns="http://www.w3.org/2000/svg"><rect width="380" height="210" fill="#ffffff" /><line x1={scale(min)} x2={scale(max)} y1="100" y2="100" stroke={COLORS.slate} strokeWidth="2" /><line x1={scale(min)} x2={scale(min)} y1="82" y2="118" stroke={COLORS.slate} strokeWidth="2" /><line x1={scale(max)} x2={scale(max)} y1="82" y2="118" stroke={COLORS.slate} strokeWidth="2" /><rect x={scale(q1)} y="65" width={Math.max(2, scale(q3) - scale(q1))} height="70" rx="5" fill="#dbeafe" stroke={COLORS.primary} strokeWidth="2" /><line x1={scale(median)} x2={scale(median)} y1="65" y2="135" stroke={COLORS.rose} strokeWidth="3" />{[min, q1, median, q3, max].map((value, index) => <text key={`${value}-${index}`} x={scale(value)} y={index % 2 ? 158 : 178} textAnchor="middle" fontSize="9" fill="#64748b">{formatScore(value)}</text>)}</svg>;
}

function SummaryMetric({ label, value, tone }: { label: string; value: string; tone: "primary" | "accent" | "warning" | "danger" }) { return <div className="rounded-[9px] border px-3 py-3"><strong className={cn("text-[18px] leading-6", tone === "primary" && "text-primary", tone === "accent" && "text-teal-500", tone === "warning" && "text-amber-500", tone === "danger" && "text-rose-500")}>{value}</strong><span className="mt-1 block text-[10px] font-medium text-muted-foreground">{label}</span></div>; }

function buildScoreDistribution(students: StudentSummary[]) { const buckets = [{ label: "<60", min: -Infinity, max: 60 }, { label: "60–69", min: 60, max: 70 }, { label: "70–79", min: 70, max: 80 }, { label: "80–89", min: 80, max: 90 }, { label: "90–100", min: 90, max: Infinity }]; return buckets.map((bucket) => ({ ...bucket, count: students.filter((student) => student.percent !== null && student.percent >= bucket.min && student.percent < bucket.max).length })); }
function buildPassComposition(students: StudentSummary[]) { return [{ name: "≥60%", value: students.filter((student) => student.percent !== null && student.percent >= 60).length }, { name: "<60%", value: students.filter((student) => student.percent !== null && student.percent < 60).length }, { name: "—", value: students.filter((student) => student.percent === null).length }]; }
function buildConfidenceScatter(students: StudentSummary[]) { return students.filter((student) => student.percent !== null && student.avgConfidence !== null).map((student) => ({ name: student.name, score: student.percent, confidence: normalizeConfidence(student.avgConfidence)! * 100, review: studentNeedsReview(student) })); }
function buildQuestionPerformance(questions: QuestionSummary[], studentIds: Set<string>) { return questions.map((question) => { const entries = question.entries.filter((entry) => studentIds.has(entry.student.id)); const percents = entries.map((entry) => entry.correction.max_score > 0 ? (effectiveCorrectionScore(entry.correction) / entry.correction.max_score) * 100 : null).filter((value): value is number => value !== null); return { label: question.label, scoreRate: averageOrNull(percents) ?? 0, reviewCount: entries.filter((entry) => correctionNeedsFormalReview(entry.correction)).length }; }); }
function matchesScope(student: StudentSummary, scope: ScopeFilter): boolean { if (scope === "all") return true; if (scope === "review") return studentNeedsReview(student); if (student.percent === null) return false; return scope === "pass" ? student.percent >= 60 : student.percent < 60; }
function studentNeedsReview(student: StudentSummary): boolean { return student.corrections.some(correctionNeedsFormalReview); }
function correctionNeedsFormalReview(correction: Correction): boolean { const confidence = normalizeConfidence(correction.confidence); return (confidence !== null && confidence < 0.65) || correction.requires_human_review || correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high") || correction.synthesis_method === "all_failed" || correction.synthesis_method === "quota_exhausted" || correctionHasDisagreement(correction); }
function correctionHasDisagreement(correction: Correction): boolean { if (correction.review_reasons?.some((reason) => reason === "high_indecisiveness" || reason === "score_spread_high")) return true; const scores = correction.expert_results?.map((result) => Number(result.score)).filter(Number.isFinite) ?? []; return scores.length > 1 && correction.max_score > 0 && Math.max(...scores) - Math.min(...scores) > Math.max(1, correction.max_score * 0.25); }
function chartMetadata(locale: Locale, sample: number, version: number, scope: ScopeFilter, extra = ""): string[] { return [tx(locale, `样本 ${sample}`, `Sample ${sample}`), tx(locale, `正式结果 v${version}`, `Final Results v${version}`), scopeLabel(locale, scope), extra].filter(Boolean); }
function scopeLabel(locale: Locale, scope: ScopeFilter): string { return scope === "pass" ? tx(locale, "仅及格", "Passed only") : scope === "fail" ? tx(locale, "仅未及格", "Failed only") : scope === "review" ? tx(locale, "含必审信号", "With review signals") : tx(locale, "全部学生", "All students"); }
function normalizeScope(value: string | null): ScopeFilter { return value === "pass" || value === "fail" || value === "review" ? value : "all"; }
function normalizeConfidence(value: number | null | undefined): number | null { if (typeof value !== "number" || !Number.isFinite(value)) return null; return value > 1 ? value / 100 : value; }
function averageOrNull(values: number[]): number | null { return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null; }
function medianOrNull(values: number[]): number | null { if (!values.length) return null; const sorted = [...values].sort((a, b) => a - b); const middle = Math.floor(sorted.length / 2); return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2; }
function quantile(sorted: number[], fraction: number): number { const index = (sorted.length - 1) * fraction; const lower = Math.floor(index); const upper = Math.ceil(index); return sorted[lower] + (sorted[upper] - sorted[lower]) * (index - lower); }
function traceSeries(trace: ChartTrace) { const labels = (trace.x ?? trace.labels ?? []).slice(0, 50); const values = traceValues(trace); return values.map((value, index) => ({ label: String(labels[index] ?? index + 1), value })); }
function traceValues(trace: ChartTrace): number[] { return (trace.y ?? trace.values ?? []).slice(0, 50).map(numeric).filter((value): value is number => value !== null); }
function tracePoints(trace: ChartTrace) { const xs = (trace.x ?? []).slice(0, 50); const ys = (trace.y ?? []).slice(0, 50); return ys.map((value, index) => ({ x: numeric(xs[index]) ?? index + 1, y: numeric(value) ?? 0, label: String(xs[index] ?? index + 1) })); }
function numeric(value: unknown): number | null { const number = Number(value); return Number.isFinite(number) ? number : null; }
function isAllowedTrace(trace: ChartTrace): boolean { return ["bar", "scatter", "pie", "histogram", "box"].includes(trace.type); }
function heatColor(percent: number | null): string { if (percent === null) return "#f1f5f9"; if (percent < 60) return "#ffe4e6"; if (percent < 75) return "#fef3c7"; if (percent < 90) return "#dbeafe"; return "#ccfbf1"; }
function compactLabel(value: string, max: number): string { return value.length > max ? `${value.slice(0, max - 1)}…` : value; }

async function exportChartPng(id: string, title: string, locale: Locale): Promise<void> {
  const container = document.querySelector(`[data-chart-export="${CSS.escape(id)}"]`);
  const svg = container?.querySelector("svg");
  if (!svg) { toast.error(tx(locale, "当前图表没有可导出的 SVG", "This chart has no exportable SVG")); return; }
  const bounds = svg.getBoundingClientRect();
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg"); clone.setAttribute("width", String(Math.max(1, bounds.width))); clone.setAttribute("height", String(Math.max(1, bounds.height)));
  const source = new XMLSerializer().serializeToString(clone);
  const url = URL.createObjectURL(new Blob([source], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => { image.onload = () => resolve(); image.onerror = () => reject(new Error("image_load_failed")); image.src = url; });
    const scale = Math.min(2, window.devicePixelRatio || 1);
    const canvas = document.createElement("canvas"); canvas.width = Math.ceil(bounds.width * scale); canvas.height = Math.ceil(bounds.height * scale);
    const context = canvas.getContext("2d"); if (!context) throw new Error("canvas_unavailable"); context.scale(scale, scale); context.fillStyle = "#ffffff"; context.fillRect(0, 0, bounds.width, bounds.height); context.drawImage(image, 0, 0, bounds.width, bounds.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png")); if (!blob) throw new Error("png_encode_failed");
    const downloadUrl = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = downloadUrl; anchor.download = `${safeFileName(title || id)}.png`; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
  } catch { toast.error(tx(locale, "PNG 导出失败", "PNG export failed")); } finally { URL.revokeObjectURL(url); }
}

function safeFileName(value: string): string { return value.replace(/[\\/:*?"<>|]+/g, "-").trim().slice(0, 80) || "chart"; }
const tooltipStyle = { borderRadius: 8, borderColor: COLORS.grid, fontSize: 11 };
function tx(locale: Locale, zh: string, en: string): string { return locale === "en-US" ? en : zh; }

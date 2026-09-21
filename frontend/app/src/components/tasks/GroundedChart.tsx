import { Bar, BarChart, CartesianGrid, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import type { ChartAnalyticsResult, ChartTrace } from "@/types";

export const chartNumber = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
/** Never drop null y values independently of their x labels. */
export function groundedSeries(trace: ChartTrace) {
  const xs = trace.x ?? trace.labels ?? [];
  return (trace.y ?? trace.values ?? []).map((y, index) => ({ label: String(xs[index] ?? index + 1), value: chartNumber(y) }));
}
export function GroundedTrace({ trace, locale = "zh-CN" }: { trace: ChartTrace; locale?: string }) {
  const values = groundedSeries(trace);
  const chart = trace.type === "pie" ? <PieChart><Pie isAnimationActive={false} data={values} dataKey="value" nameKey="label" label /><Tooltip /></PieChart>
    : trace.type === "scatter" ? <ScatterChart><CartesianGrid strokeDasharray="3 3" /><XAxis type="number" dataKey="x" /><YAxis type="number" dataKey="y" /><Tooltip /><Scatter isAnimationActive={false} name={trace.name} data={(trace.y ?? []).map((v, i) => ({ x: chartNumber(trace.x?.[i]), y: chartNumber(v) })).filter(p => p.x !== null && p.y !== null)} fill="currentColor" /></ScatterChart>
    : trace.type === "line" ? <LineChart data={values}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="label" /><YAxis /><Tooltip /><Line isAnimationActive={false} name={trace.name} dataKey="value" connectNulls={false} stroke="currentColor" /></LineChart>
    : trace.type === "box" ? null
    : <BarChart data={trace.type === "histogram" ? histogram(values.map(v => v.value).filter((v): v is number => v !== null)) : values}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="label" /><YAxis /><Tooltip /><Bar isAnimationActive={false} name={trace.name} dataKey="value" fill="currentColor" /></BarChart>;
  return <section className="min-w-0 rounded-lg border p-3 text-primary" aria-label={trace.name}>
    <h5 className="mb-2 text-xs font-semibold text-foreground">{trace.name}</h5>
    {chart ? <ResponsiveContainer width="100%" height={250}>{chart}</ResponsiveContainer> : <Box locale={locale} values={values.map(v => v.value).filter((v): v is number => v !== null)} />}
  </section>;
}
function histogram(values: number[]) {
  if (!values.length) return [];
  const min = Math.min(...values), max = Math.max(...values), count = Math.min(20, Math.max(1, Math.ceil(Math.sqrt(values.length))));
  if (min === max) return [{ label: String(min), value: values.length }];
  const width = (max - min) / count;
  const bins = Array.from({ length: count }, (_, i) => ({ label: `${(min + i * width).toFixed(2)}–${(min + (i + 1) * width).toFixed(2)}`, value: 0 }));
  values.forEach(v => { bins[Math.min(count - 1, Math.floor((v - min) / width))].value += 1; });
  return bins;
}
function Box({ values, locale }: { values: number[]; locale: string }) {
  if (!values.length) return <p className="text-xs text-muted-foreground">{locale === "zh-CN" ? "暂无已知数值" : "No known numeric values"}</p>;
  const sorted = [...values].sort((a, b) => a - b);
  const q = (p: number) => { const at = (sorted.length - 1) * p, lo = Math.floor(at); return sorted[lo] + (sorted[Math.ceil(at)] - sorted[lo]) * (at - lo); };
  const [min, first, median, third, max] = [q(0), q(.25), q(.5), q(.75), q(1)];
  const x = (value: number) => max === min ? 250 : 40 + 420 * (value - min) / (max - min);
  return <svg viewBox="0 0 500 170" role="img" aria-label={`min ${min}, Q1 ${first}, median ${median}, Q3 ${third}, max ${max}`}>
    <line x1={x(min)} x2={x(max)} y1={75} y2={75} stroke="currentColor" />
    <rect x={x(first)} y={50} width={Math.max(1, x(third) - x(first))} height={50} fill="none" stroke="currentColor" />
    {[min, median, max].map((v, i) => <line key={i} x1={x(v)} x2={x(v)} y1={50} y2={100} stroke="currentColor" />)}
    <text x={40} y={135} fontSize={12}>{min}</text><text x={250} y={135} textAnchor="middle" fontSize={12}>{median}</text><text x={460} y={135} textAnchor="end" fontSize={12}>{max}</text>
  </svg>;
}
export function GroundedChart({ result, locale = "zh-CN" }: { result: ChartAnalyticsResult; locale?: string }) {
  return <div className="mt-3" data-grounded-chart><h4 className="text-sm font-semibold">{result.title}</h4>
    <div className="mt-2 grid gap-3 lg:grid-cols-2">{result.traces.map((trace, i) => <GroundedTrace key={i} trace={trace} locale={locale} />)}</div>
  </div>;
}

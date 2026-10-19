import { lazy, Suspense } from "react";
import type { GroundedAskExecution } from "@/types";
const GroundedChart = lazy(() => import("./GroundedChart").then(module => ({default: module.GroundedChart})));

/** Successful queries update the existing table or chart. Planning evidence stays off-screen. */
export function GroundedAskAnswer({ execution, locale, renderChart = true }: {
  execution?: GroundedAskExecution | null; locale: string; renderChart?: boolean;
}) {
  if (!execution) return null;
  const zh = locale === "zh-CN";
  if (!execution.recognized) {
    // Keep useful clarification, but do not present planner diagnostics as copy.
    const internalDiagnostic = /查询计划|执行校验|字段绑定|未知标识|\b(?:sql|sqlite|student_id|task_id|q_id|fingerprint)\b|\b(?:query|selection) (?:plan|validation|requires|contains unknown)/i.test(execution.explanation);
    const explanation = internalDiagnostic
      ? "未能完成查询，请缩小范围或换一种表达。 / Could not complete the query. Narrow it or rephrase it."
      : execution.explanation;
    const [chinese, english] = explanation.split(" / ");
    return <p role="status" className="mt-2 text-xs text-amber-700">{zh ? chinese : english || chinese}</p>;
  }
  if (!renderChart || !execution.chart) return null;
  return <Suspense fallback={<p className="mt-2 text-xs">{zh ? "正在加载图表…" : "Loading chart…"}</p>}>
    <GroundedChart result={execution.chart} locale={locale} />
  </Suspense>;
}

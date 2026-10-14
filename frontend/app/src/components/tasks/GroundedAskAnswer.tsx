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
    // Server validation messages use the established Chinese / English contract.
    const [chinese, english] = execution.explanation.split(" / ");
    return <p role="status" className="mt-2 text-xs text-amber-700">{zh ? chinese : english || chinese}</p>;
  }
  if (!renderChart || !execution.chart) return null;
  return <Suspense fallback={<p className="mt-2 text-xs">{zh ? "正在加载图表…" : "Loading chart…"}</p>}>
    <GroundedChart result={execution.chart} locale={locale} />
  </Suspense>;
}

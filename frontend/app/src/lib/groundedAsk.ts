import type { FilterIntentResult, GroundedAskExecution } from "@/types";

/** Select by stable IDs and preserve the order produced by the database. */
export function groundedRows<T>(rows: T[], intent: FilterIntentResult | null | undefined,
  kind: "students" | "questions" | "tasks", id: (row: T) => string): T[] {
  const selection = intent?.execution?.recognized ? intent.execution.selection : null;
  if (!selection || selection.kind !== kind) return rows;
  const byId = new Map(rows.map((row) => [id(row), row]));
  return selection.ids.flatMap((key) => { const row = byId.get(key); return row === undefined ? [] : [row]; });
}

export function hasGroundedOrder(intent: FilterIntentResult | null | undefined): boolean {
  return Boolean(intent?.execution?.recognized && intent.execution.selection);
}

/** Verify response structure before any selection affects a displayed table. */
export function isGroundedExecution(value: unknown): value is GroundedAskExecution {
  if (!value || typeof value !== "object") return false;
  const v = value as GroundedAskExecution;
  if (typeof v.recognized !== "boolean" || typeof v.explanation !== "string" || !v.data
      || !Array.isArray(v.data.columns) || !v.data.columns.every(c => typeof c === "string")
      || !Array.isArray(v.data.rows) || v.data.rows.length > 5000
      || !v.data.rows.every(row => Array.isArray(row) && row.length === v.data.columns.length
        && row.every(cell => cell === null || typeof cell === "string" || typeof cell === "number" && Number.isFinite(cell)))) return false;
  if (!["students", "questions", "tasks", "table", "chart", "clarification"].includes(v.kind)
      || v.data.columns.length > 100 || new Set(v.data.columns).size !== v.data.columns.length) return false;
  if (v.selection && (!["students", "questions", "tasks"].includes(v.selection.kind)
      || !Array.isArray(v.selection.ids) || !v.selection.ids.every(id => typeof id === "string"))) return false;
  if (!v.recognized && (v.selection || v.chart)) return false;
  if (v.chart) {
    const chart = v.chart;
    if (chart.mode !== "chart" || typeof chart.title !== "string" || !Array.isArray(chart.traces) || chart.traces.length > 12) return false;
    for (const trace of chart.traces) {
      if (!["bar", "line", "scatter", "pie", "histogram", "box"].includes(trace.type)) return false;
      const xs = trace.x ?? trace.labels, ys = trace.y ?? trace.values;
      if (!Array.isArray(xs) || !Array.isArray(ys) || xs.length !== ys.length || xs.length > 2000
          || !xs.every(x => x === null || typeof x === "string" || typeof x === "number" && Number.isFinite(x))
          || !ys.every(y => y === null || typeof y === "number" && Number.isFinite(y))) return false;
    }
  }
  return true;
}

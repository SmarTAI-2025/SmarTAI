/** Keep absent values last in either direction; never coerce them to zero. */
export function compareValues(a: string | number | null | undefined, b: string | number | null | undefined, direction: "asc" | "desc" = "asc"): number {
  const absent = (value: typeof a) => value == null || (typeof value === "number" && !Number.isFinite(value));
  if (absent(a) || absent(b)) return Number(absent(a)) - Number(absent(b));
  const compared = typeof a === "number" && typeof b === "number"
    ? a - b
    : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  return direction === "asc" ? compared : -compared;
}

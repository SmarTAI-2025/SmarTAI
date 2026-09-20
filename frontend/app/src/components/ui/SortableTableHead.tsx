import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { HTMLAttributes, ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { compareValues } from "@/lib/sortValues";

export interface ColumnSort { key: string; direction: "asc" | "desc" }
export function toggleColumnSort(current: ColumnSort | null, key: string): ColumnSort {
  return { key, direction: current?.key === key && current.direction === "asc" ? "desc" : "asc" };
}
export function sortColumnRows<T>(rows: T[], sort: ColumnSort | null, value: (row: T, column: string) => string | number | null | undefined): T[] {
  return sort ? [...rows].sort((a, b) => compareValues(value(a, sort.key), value(b, sort.key), sort.direction)) : rows;
}

/** URLs preserve the order through detail navigation; every new header starts at page 1. */
export function useColumnSort(keys: string[], fallback: ColumnSort | null, onChange: () => void) {
  const [params, setParams] = useSearchParams();
  let current = fallback;
  try {
    const parsed: unknown = JSON.parse(params.get("column_sort") ?? "null");
    if (Array.isArray(parsed) && parsed.length === 2 && keys.includes(parsed[0]) && ["asc", "desc"].includes(parsed[1])) {
      current = { key: parsed[0], direction: parsed[1] };
    }
  } catch { /* Invalid/tampered URLs fall back to the supported natural-language order. */ }
  const effective = current && keys.includes(current.key) ? current : null;
  function toggle(key: string) {
    if (!keys.includes(key)) return;
    onChange();
    const nextSort = toggleColumnSort(effective, key);
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("column_sort", JSON.stringify([key, nextSort.direction]));
      next.delete("page");
      return next;
    }, { replace: true });
  }
  return { current: effective, toggle };
}

export function directionFor(sort: ColumnSort | null, key: string) {
  return sort?.key === key ? sort.direction : null;
}
export function sortLabel(label: string, direction?: ColumnSort["direction"] | null, locale?: string): string {
  const zh = locale ? locale === "zh-CN" : /[\u3400-\u9fff]/.test(label);
  const status = zh
    ? direction === "asc" ? "当前升序；点击降序" : direction === "desc" ? "当前降序；点击升序" : "当前未排序；点击升序"
    : direction === "asc" ? "currently ascending; click to sort descending" : direction === "desc" ? "currently descending; click to sort ascending" : "currently unsorted; click to sort ascending";
  return `${label}${zh ? "，" : ": "}${status}`;
}

export function SortButton({ children, label, direction, onSort, locale }: {
  children: ReactNode; label: string; direction?: ColumnSort["direction"] | null; onSort: () => void; locale?: string;
}) {
  const Icon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ArrowUpDown;
  return <button type="button" onClick={onSort} aria-label={sortLabel(label, direction, locale)}
    className="inline-flex max-w-full items-center gap-1 rounded-sm text-left text-inherit outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
    {children}<Icon aria-hidden="true" className="h-3 w-3 shrink-0" />
  </button>;
}

export function SortableTableHead({ children, label, direction, onSort, locale, secondary, as = "th", ...props }: Omit<HTMLAttributes<HTMLTableCellElement>, "children"> & {
  children: ReactNode; label?: string; direction?: ColumnSort["direction"] | null; onSort: () => void; locale?: string; secondary?: ReactNode; as?: "th" | "div";
}) {
  const Element = as;
  const name = label ?? (typeof children === "string" ? children : "Column");
  return <Element {...props} role={as === "div" ? "columnheader" : undefined} aria-label={name}
    aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}>
    <SortButton label={name} direction={direction} onSort={onSort} locale={locale}>{children}</SortButton>{secondary}
  </Element>;
}

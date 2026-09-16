import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export type SortDirection = "asc" | "desc";

export function sortableTableHeadLabel(label: string, direction?: SortDirection | null): string {
  const chinese = /[\u3400-\u9fff]/.test(label);
  const state = chinese
    ? direction === "asc" ? "当前升序；点击降序" : direction === "desc" ? "当前降序；点击升序" : "当前未排序；点击升序"
    : direction === "asc" ? "currently ascending; click to sort descending" : direction === "desc" ? "currently descending; click to sort ascending" : "currently unsorted; click to sort ascending";
  return chinese ? `${label}，${state}` : `${label}: ${state}`;
}

export function SortableTableHead({ children, direction, onSort, className, as = "th", sortLabel, ...props }: Omit<HTMLAttributes<HTMLTableCellElement>, "children"> & {
  children: ReactNode;
  direction?: SortDirection | null;
  onSort: () => void;
  as?: "th" | "div";
  sortLabel?: string;
}) {
  const Element = as;
  const Icon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ArrowUpDown;
  const label = sortLabel ?? (typeof children === "string" ? children : "column");
  const accessibleLabel = sortableTableHeadLabel(label, direction);
  return (
    <Element {...props} role={as === "div" ? "columnheader" : undefined} aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"} className={className}>
      <button type="button" onClick={onSort} aria-label={accessibleLabel} className={cn("inline-flex max-w-full items-center justify-center gap-1 rounded-sm text-inherit outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-ring", direction && "text-primary")}>
        {children}<Icon aria-hidden="true" className="h-3 w-3 shrink-0" />
      </button>
    </Element>
  );
}

export interface ColumnSort { key: string; direction: SortDirection }
export function toggleColumnSort(current: ColumnSort | null, key: string): ColumnSort {
  return { key, direction: current?.key === key && current.direction === "asc" ? "desc" : "asc" };
}

/** Unknown numeric values always remain last in either direction. */
export function compareSortableValues(left: string | number | null | undefined, right: string | number | null | undefined, direction: SortDirection): number {
  const missing = (value: typeof left) => value === null || value === undefined || (typeof value === "number" && !Number.isFinite(value));
  if (missing(left) || missing(right)) return Number(missing(left)) - Number(missing(right));
  const result = typeof left === "number" && typeof right === "number" ? left - right : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" });
  return direction === "asc" ? result : -result;
}

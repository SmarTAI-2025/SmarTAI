import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import type { ComponentPropsWithoutRef } from "react";
import { cn } from "@/lib/cn";

export type TableSortDirection = "asc" | "desc" | null;

interface SortableTableHeadProps extends Omit<ComponentPropsWithoutRef<"th">, "children"> {
  label: string;
  direction: TableSortDirection;
  onSort: () => void;
  ariaLabel?: string;
  buttonClassName?: string;
}

interface SortableHeaderButtonProps extends Omit<ComponentPropsWithoutRef<"button">, "children" | "onClick"> {
  label: string;
  direction: TableSortDirection;
  onSort: () => void;
  ariaLabel?: string;
}

export function SortableTableHead({
  label,
  direction,
  onSort,
  ariaLabel,
  className,
  buttonClassName,
  ...props
}: SortableTableHeadProps) {
  return (
    <th
      {...props}
      aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}
      className={cn(className)}
    >
      <SortableHeaderButton label={label} direction={direction} onSort={onSort} ariaLabel={ariaLabel} className={buttonClassName} />
    </th>
  );
}

export function SortableHeaderButton({ label, direction, onSort, ariaLabel, className, ...props }: SortableHeaderButtonProps) {
  const directionLabel = direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "not sorted";
  const Icon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ChevronsUpDown;
  return (
    <button
      {...props}
      type="button"
      onClick={onSort}
      aria-label={ariaLabel ?? `${label}, ${directionLabel}. Activate to change sort direction.`}
      title={ariaLabel ?? `${label}, ${directionLabel}`}
      className={cn(
        "inline-flex max-w-full items-center gap-1 text-left outline-none transition-colors hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-primary/50",
        className,
      )}
    >
      <span className="truncate">{label}</span>
      <Icon aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
    </button>
  );
}

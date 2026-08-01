import { Check, CircleCheck, CircleX, MessageSquareText, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";

export type MatrixStatusTone = "ok" | "reviewed" | "warning" | "error" | "note";

const ICONS = {
  ok: Check,
  reviewed: CircleCheck,
  warning: TriangleAlert,
  error: CircleX,
  note: MessageSquareText,
} as const;

/**
 * Compact, accessible status block for dense student-by-question matrices.
 * The icon preserves room for ten or more questions; the complete state stays
 * available to screen readers and pointer users through aria-label/title.
 */
export function MatrixStatusCell({
  to,
  label,
  tone,
}: {
  to: string;
  label: string;
  tone: MatrixStatusTone;
}) {
  const Icon = ICONS[tone];
  return (
    <Link
      to={to}
      aria-label={label}
      title={label}
      className={cn(
        "flex h-8 w-full min-w-10 items-center justify-center rounded-[7px] outline-none transition hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
        tone === "ok" && "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/70 dark:text-emerald-200",
        tone === "reviewed" && "bg-blue-100 text-primary dark:bg-blue-950/70 dark:text-blue-200",
        tone === "warning" && "bg-amber-100 text-amber-700 dark:bg-amber-950/70 dark:text-amber-200",
        tone === "error" && "bg-red-100 text-red-600 dark:bg-red-950/60 dark:text-red-200",
        tone === "note" && "bg-sky-100 text-sky-700 dark:bg-sky-950/70 dark:text-sky-200",
      )}
    >
      <Icon aria-hidden="true" className="h-4 w-4" strokeWidth={2.25} />
      <span className="sr-only">{label}</span>
    </Link>
  );
}

import { CircleHelp } from "lucide-react";
import { cn } from "@/lib/cn";

export function HelpTooltip({
  label,
  className,
}: {
  label: string;
  className?: string;
}) {
  return (
    <span className={cn("group relative inline-flex align-middle", className)}>
      <button
        type="button"
        className="inline-flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground transition hover:bg-muted hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        aria-label={label}
      >
        <CircleHelp className="h-3.5 w-3.5" />
      </button>
      <span className="pointer-events-none absolute left-1/2 top-7 z-20 hidden w-64 -translate-x-1/2 rounded-md border bg-card p-2 text-left text-xs leading-5 text-card-foreground shadow-lg group-hover:block group-focus-within:block">
        {label}
      </span>
    </span>
  );
}

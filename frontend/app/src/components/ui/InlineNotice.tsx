import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/cn";

type NoticeTone = "info" | "success" | "warning" | "danger" | "neutral";

const toneClasses: Record<NoticeTone, string> = {
  info: "border-primary/30 bg-primary/5 text-primary",
  success: "border-accent/30 bg-accent/5 text-accent",
  warning: "border-warning/30 bg-warning/5 text-warning",
  danger: "border-danger/30 bg-danger/5 text-danger",
  neutral: "border-border bg-muted/30 text-muted-foreground",
};

const icons = {
  info: Info,
  success: CheckCircle2,
  warning: TriangleAlert,
  danger: AlertCircle,
  neutral: Info,
} satisfies Record<NoticeTone, typeof Info>;

export function InlineNotice({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: NoticeTone;
  title?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const Icon = icons[tone];

  return (
    <div className={cn("rounded-lg border p-3", toneClasses[tone], className)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0">
            {title ? <p className="text-sm font-semibold">{title}</p> : null}
            <div className={cn("text-sm leading-6", title ? "mt-1" : "")}>{children}</div>
          </div>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
    </div>
  );
}

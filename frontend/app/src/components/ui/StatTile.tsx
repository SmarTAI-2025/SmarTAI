import type { ComponentType } from "react";
import { cn } from "@/lib/cn";

export function StatTile({
  icon: Icon,
  label,
  value,
  tone = "primary",
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string | number;
  tone?: "primary" | "accent" | "warning" | "danger";
}) {
  const toneClass = {
    primary: "bg-primary text-primary-foreground",
    accent: "bg-accent text-white",
    warning: "bg-warning text-white",
    danger: "bg-danger text-white",
  }[tone];

  return (
    <div className="flex items-center gap-3 rounded-lg border bg-card p-4">
      <div className={cn("flex h-10 w-10 items-center justify-center rounded-md", toneClass)}>
        <Icon className="h-5 w-5" />
      </div>
      <div>
        <div className="text-sm text-muted-foreground">{label}</div>
        <div className="text-xl font-semibold">{value}</div>
      </div>
    </div>
  );
}


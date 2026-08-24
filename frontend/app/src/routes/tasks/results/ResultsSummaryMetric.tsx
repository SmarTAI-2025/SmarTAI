import { cn } from "@/lib/cn";

export type ResultMetricTone = "primary" | "accent" | "secondary" | "warning" | "danger";

export function ResultsSummaryMetric({
  label,
  value,
  tone,
  size = "compact",
}: {
  label: string;
  value: string;
  tone: ResultMetricTone;
  size?: "compact" | "large";
}) {
  return (
    <div
      className={cn(
        "rounded-[9px] border",
        size === "large" ? "px-4 py-4" : "px-3 py-3",
      )}
      data-result-tone={tone}
    >
      <strong className={cn("text-primary", size === "large" ? "text-[26px] leading-8" : "text-[18px] leading-6")}>{value}</strong>
      <span className={cn("block font-medium text-muted-foreground", size === "large" ? "mt-1 text-[12px]" : "mt-1 text-[10px]")}>{label}</span>
    </div>
  );
}

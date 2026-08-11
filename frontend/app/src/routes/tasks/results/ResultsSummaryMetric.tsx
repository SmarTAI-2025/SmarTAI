import { cn } from "@/lib/cn";

export type ResultMetricTone = "primary" | "accent" | "secondary" | "warning" | "danger";

const TONE_STYLES: Record<ResultMetricTone, { card: string; value: string; accent: string }> = {
  primary: {
    card: "border-[#dbe0ff] bg-[#f4f5ff]",
    value: "text-[#4f61c9]",
    accent: "bg-[#7c8cf8]",
  },
  accent: {
    card: "border-[#cfece4] bg-[#effaf7]",
    value: "text-[#247d69]",
    accent: "bg-[#5ec7ae]",
  },
  secondary: {
    card: "border-[#e2daf9] bg-[#f7f3ff]",
    value: "text-[#7058b2]",
    accent: "bg-[#a995e8]",
  },
  warning: {
    card: "border-[#f3dfbf] bg-[#fff8ee]",
    value: "text-[#a56524]",
    accent: "bg-[#f3b780]",
  },
  danger: {
    card: "border-[#f2d5da] bg-[#fff3f5]",
    value: "text-[#b64f60]",
    accent: "bg-[#f08f9b]",
  },
};

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
  const styles = TONE_STYLES[tone];

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-[9px] border shadow-[0_8px_22px_-20px_rgba(40,56,99,0.55)]",
        styles.card,
        size === "large" ? "px-4 py-4" : "px-3 py-3",
      )}
      data-result-tone={tone}
    >
      <span aria-hidden="true" className={cn("absolute inset-y-0 left-0 w-1", styles.accent)} />
      <strong className={cn(size === "large" ? "text-[26px] leading-8" : "text-[18px] leading-6", styles.value)}>{value}</strong>
      <span className={cn("block font-medium text-muted-foreground", size === "large" ? "mt-1 text-[12px]" : "mt-1 text-[10px]")}>{label}</span>
    </div>
  );
}

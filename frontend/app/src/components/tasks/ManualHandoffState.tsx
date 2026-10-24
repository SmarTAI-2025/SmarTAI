import { LoaderCircle } from "lucide-react";
import type { Locale } from "@/i18n/messages";

export type ManualReviewStage = "questions" | "submissions" | "grading";

/** Visible immediately, including while the handoff request and cache refresh are pending. */
export function ManualHandoffState({ stage, locale }: {
  stage: ManualReviewStage; locale: Locale;
}) {
  const zh = locale === "zh-CN";
  const destination = stage === "questions" ? (zh ? "题目审核" : "question review")
    : stage === "submissions" ? (zh ? "作答校对" : "submission review")
      : (zh ? "批改复核" : "grading review");
  return <div role="status" aria-live="polite" aria-busy="true" className="flex items-start gap-3 rounded-lg bg-primary/5 p-4">
    <LoaderCircle aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0 animate-spin text-primary" />
    <div className="min-w-0">
    <p className="text-sm font-semibold text-foreground">{zh ? "正在打开人工复核" : "Opening manual review"}</p>
    <p className="mt-1 text-sm leading-6 text-muted-foreground">{zh
      ? `正在恢复已保存的结果，准备进入${destination}。`
      : `Restoring saved results to open ${destination}.`}</p>
    </div>
  </div>;
}

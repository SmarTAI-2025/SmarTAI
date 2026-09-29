import type { KnowledgeIngestionSummary } from "@/types/personalKnowledge";

export function knowledgeStatusLabel(status: string, zh: boolean): string {
  const labels: Record<string, [string, string]> = {
    queued: ["等待识别", "Queued"], processing: ["识别中", "Recognizing"],
    partial: ["部分可用，未全部完成", "Partially available"], failed: ["识别失败", "Recognition failed"],
    paused: ["已暂停", "Paused"], cancelled: ["已暂停", "Paused"],
    complete: ["识别完成", "Recognition complete"], ready: ["识别完成", "Recognition complete"],
    complete_with_warning: ["识别完成，有待核对", "Complete, with warnings"],
  };
  return labels[status]?.[zh ? 0 : 1] ?? (zh ? "状态待确认" : "Status unavailable");
}

export function knowledgeProgress(summary?: KnowledgeIngestionSummary) {
  const total = summary?.total_pages;
  if (typeof total !== "number" || !Number.isFinite(total) || total <= 0) return null;
  const processed = Math.min(total, Math.max(0, summary?.processed_pages ?? 0));
  return { total, processed, percent: Math.floor(processed / total * 100) };
}

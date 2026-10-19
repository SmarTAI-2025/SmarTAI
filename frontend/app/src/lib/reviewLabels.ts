import { normalizeFilterType } from "./taskFilterIntent";

export const QUESTION_TYPES = ["calculation", "programming", "proof", "concept", "choice", "fill_blank", "short_answer", "other"];

/** Translate standard labels, preserving custom teacher-provided descriptions. */
export function questionTypeLabel(value: string | undefined, locale: string): string {
  const labels: Record<string, [string, string]> = {
    calculation: ["计算题", "Calculation"], programming: ["编程题", "Programming"],
    proof: ["证明题", "Proof"], concept: ["概念题", "Concept"], choice: ["选择题", "Multiple choice"],
    fill_blank: ["填空题", "Fill in the blank"], short_answer: ["简答题", "Short answer"],
    other: ["其他", "Other"],
  };
  const label = labels[normalizeFilterType(value ?? "")];
  return label ? label[locale === "zh-CN" ? 0 : 1] : value || (locale === "zh-CN" ? "未分类" : "Uncategorized");
}

export function testCaseLabel(title: string | undefined, index: number, locale: string): string {
  const standard = title?.match(/^(?:样例|Example)\s*(\d+)$/i);
  return title && !standard ? title : `${locale === "zh-CN" ? "样例" : "Example"} ${standard?.[1] ?? index + 1}`;
}

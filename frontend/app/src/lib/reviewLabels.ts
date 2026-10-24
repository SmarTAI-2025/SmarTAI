import { normalizeFilterType } from "./taskFilterIntent";

export const QUESTION_TYPES = ["calculation", "concept", "reasoning", "programming", "other"];

export function hasQuestionType(value: string | undefined): boolean {
  return QUESTION_TYPES.includes(normalizeFilterType(value ?? ""));
}

/** Missing or unrecognized types require an explicit teacher selection. */
export function questionTypeLabel(value: string | undefined, locale: string): string {
  const labels: Record<string, [string, string]> = {
    calculation: ["计算题", "Calculation"], programming: ["编程题", "Programming"],
    reasoning: ["推理题", "Reasoning"], concept: ["概念题", "Concept"],
    other: ["其他", "Other"],
  };
  const label = labels[normalizeFilterType(value ?? "")];
  return label ? label[locale === "zh-CN" ? 0 : 1] : (locale === "zh-CN" ? "请选择" : "Please select");
}

export function testCaseLabel(title: string | undefined, index: number, locale: string): string {
  const standard = title?.match(/^(?:样例|Example)\s*(\d+)$/i);
  return title && !standard ? title : `${locale === "zh-CN" ? "样例" : "Example"} ${standard?.[1] ?? index + 1}`;
}

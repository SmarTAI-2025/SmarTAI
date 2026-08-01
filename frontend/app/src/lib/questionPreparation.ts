import type { ProblemInfo } from "@/types";

export type QuestionPreparationSort = "number" | "missing" | "type";
export type QuestionPreparationRule =
  | "missing_answer"
  | "missing_rubric"
  | "missing_code"
  | "programming"
  | "proof"
  | "keyword"
  | "missing_first";

export interface QuestionPreparationRow {
  problem: ProblemInfo;
  label: string;
  type: string;
  reviewStatus: "confirmed" | "edited" | "needs_review";
  hasRubric: boolean;
  hasAnswer: boolean;
  hasCode: boolean;
  isProgramming: boolean;
  hasTests: boolean;
  missingCount: number;
}

export interface QuestionPreparationSelection {
  rows: QuestionPreparationRow[];
  rules: QuestionPreparationRule[];
  effectiveSort: QuestionPreparationSort;
}

const NUMBER_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

const QUERY_RULES: Array<{
  kind: Exclude<QuestionPreparationRule, "keyword">;
  pattern: RegExp;
}> = [
  {
    kind: "missing_answer",
    pattern: /缺(?:少|失)?标答|没有标答|无标答|标答缺失|missing\s+(?:reference\s+)?answer/giu,
  },
  {
    kind: "missing_rubric",
    pattern: /缺(?:少|失)?评分标准|没有评分标准|无评分标准|评分标准缺失|missing\s+(?:rubric|criterion)/giu,
  },
  {
    kind: "missing_code",
    pattern: /缺(?:少|失)?(?:示例|参考|正确)?代码|没有(?:示例|参考|正确)?代码|无(?:示例|参考|正确)?代码|missing\s+(?:solution|reference)?\s*code/giu,
  },
  {
    kind: "programming",
    pattern: /编程题?|程序题?|代码题?|programming|coding/giu,
  },
  {
    kind: "proof",
    pattern: /证明题?|proof/giu,
  },
  {
    kind: "missing_first",
    pattern: /缺失最多|缺项最多|缺失优先|most\s+missing/giu,
  },
];

const QUERY_FILLER = /(?:^|\s)(?:请|帮我|查找|筛选|显示|找出|题目|questions?|show|find|filter|with|the)(?:\s|$)|的/giu;

export function buildQuestionPreparationRows(problems: ProblemInfo[]): QuestionPreparationRow[] {
  return problems.map((problem) => {
    const hasRubric = hasText(problem.criterion);
    const hasAnswer = hasText(problem.reference_answer);
    const hasCode = hasText(problem.solution_code);
    const isProgramming = isProgrammingProblem(problem);
    const hasTests = (problem.test_cases?.length ?? 0) > 0;
    const explicitReviewStatus = (problem as ProblemInfo & { review_status?: unknown }).review_status;
    const reviewStatus = explicitReviewStatus === "confirmed"
      ? "confirmed"
      : explicitReviewStatus === "edited"
        ? "edited"
        : "needs_review";
    const missingCount =
      (reviewStatus === "confirmed" ? 0 : 1) +
      (hasRubric ? 0 : 1) +
      (hasAnswer ? 0 : 1) +
      (isProgramming && !hasCode ? 1 : 0) +
      (isProgramming && !hasTests ? 1 : 0);

    return {
      problem,
      label: problem.number?.trim() || problem.q_id,
      type: problem.type?.trim() || "",
      reviewStatus,
      hasRubric,
      hasAnswer,
      hasCode,
      isProgramming,
      hasTests,
      missingCount,
    };
  });
}

export function selectQuestionPreparationRows(
  rows: QuestionPreparationRow[],
  query: string,
  sort: QuestionPreparationSort,
): QuestionPreparationSelection {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  let keyword = normalizedQuery;
  const rules: QuestionPreparationRule[] = [];

  for (const rule of QUERY_RULES) {
    rule.pattern.lastIndex = 0;
    if (rule.pattern.test(normalizedQuery)) {
      rules.push(rule.kind);
      rule.pattern.lastIndex = 0;
      keyword = keyword.replace(rule.pattern, " ");
    }
  }

  keyword = keyword
    .replace(QUERY_FILLER, " ")
    .replace(/[，。；、,:;!?！？()（）[\]{}]/gu, " ")
    .replace(/\s+/gu, " ")
    .trim();

  if (keyword) {
    rules.push("keyword");
  }

  const filtered = rows.filter((row) => {
    if (rules.includes("missing_answer") && row.hasAnswer) return false;
    if (rules.includes("missing_rubric") && row.hasRubric) return false;
    if (rules.includes("missing_code") && (!row.isProgramming || row.hasCode)) return false;
    if (rules.includes("programming") && !row.isProgramming) return false;
    if (rules.includes("proof") && !isProofProblem(row.problem)) return false;
    if (!keyword) return true;

    const haystack = [
      row.label,
      row.type,
      row.problem.stem,
      row.problem.criterion,
      row.problem.reference_answer,
      row.problem.solution_code,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return haystack.includes(keyword);
  });

  const effectiveSort = rules.includes("missing_first") ? "missing" : sort;
  const sorted = [...filtered].sort((left, right) => compareRows(left, right, effectiveSort));
  return { rows: sorted, rules: uniqueRules(rules), effectiveSort };
}

export function stripMissingFirstRule(query: string): string {
  const rule = QUERY_RULES.find((item) => item.kind === "missing_first");
  if (!rule) return query.trim();
  rule.pattern.lastIndex = 0;
  return query.replace(rule.pattern, " ").replace(/\s+/gu, " ").trim();
}

function compareRows(
  left: QuestionPreparationRow,
  right: QuestionPreparationRow,
  sort: QuestionPreparationSort,
): number {
  if (sort === "missing") {
    const missingDifference = right.missingCount - left.missingCount;
    if (missingDifference !== 0) return missingDifference;
  }
  if (sort === "type") {
    const typeDifference = NUMBER_COLLATOR.compare(left.type, right.type);
    if (typeDifference !== 0) return typeDifference;
  }
  return NUMBER_COLLATOR.compare(left.label, right.label);
}

export function isProgrammingProblem(problem: ProblemInfo): boolean {
  const type = problem.type?.trim().toLocaleLowerCase() ?? "";
  if (type) {
    return ["编程题", "程序题", "代码题", "programming", "programming problem", "coding", "coding problem"].includes(type);
  }

  // Legacy records may lack the controlled type field. Only then use the stem
  // as a conservative fallback; a non-programming authoritative type must win.
  return /(?:编写|实现)(?:程序|代码)|write\s+(?:a\s+)?(?:program|function)|implement\s+(?:an?\s+)?(?:algorithm|function)/iu.test(problem.stem ?? "");
}

function isProofProblem(problem: ProblemInfo): boolean {
  const text = `${problem.type ?? ""} ${problem.stem ?? ""}`.toLocaleLowerCase();
  return /证明|proof|prove/u.test(text);
}

function hasText(value?: string | null): boolean {
  return Boolean(value?.trim());
}

function uniqueRules(rules: QuestionPreparationRule[]): QuestionPreparationRule[] {
  return Array.from(new Set(rules));
}

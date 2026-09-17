import type { FilterIntentResult, FilterIntentSurface } from "@/types";

export const EMPTY_FILTER_INTENT: FilterIntentResult = {
  recognized: true, min_score_percent: null, max_score_percent: null,
  pass_status: null, low_confidence: false, review_status: null,
  disagreement: false, annotated: false, sort: null,
  question_tokens: [], text_terms: [], explanation: "",
};

const studentFields = ["min_score_percent", "max_score_percent", "pass_status", "low_confidence", "review_status", "disagreement", "text_terms"];
const studentSorts = ["score_asc", "score_desc", "confidence_asc", "confidence_desc", "review_asc", "review_desc", "name_asc", "name_desc", "id_asc", "id_desc"];
const questionSorts = ["question", "question_desc", "type_asc", "type_desc", "max_score_asc", "max_score_desc", "review_asc", "review_desc"];
const capabilities: Record<FilterIntentSurface, { fields: string[]; sorts: string[] }> = {
  student_analysis: { fields: studentFields, sorts: studentSorts },
  review_overview: { fields: [...studentFields, "question_tokens", "annotated"], sorts: studentSorts },
  question_analysis: {
    fields: ["min_score_percent", "max_score_percent", "low_confidence", "review_status", "question_tokens", "question_types", "max_average_confidence", "missing_knowledge", "text_terms"],
    sorts: [...questionSorts, "score_asc", "score_desc", "confidence_asc", "confidence_desc"],
  },
  question_preparation: {
    fields: ["question_tokens", "question_types", "text_terms", "low_confidence", "min_max_score", "max_max_score", "preparation_status", "material_field", "material_status"],
    sorts: questionSorts,
  },
  submission_review: {
    fields: ["question_tokens", "question_types", "text_terms", "submission_status"],
    sorts: ["name_asc", "name_desc", "id_asc", "id_desc", "coverage_asc", "coverage_desc", "review_asc", "review_desc"],
  },
  student_answer_review: {
    fields: ["question_tokens", "question_types", "text_terms", "submission_status"],
    sorts: ["question", "question_desc"],
  },
};

/** Reject a mismatched/partial response even when frontend and backend versions differ. */
export function supportsFilterIntent(intent: FilterIntentResult, surface: FilterIntentSurface): boolean {
  if (!intent.recognized || !Array.isArray(intent.question_tokens) || !Array.isArray(intent.text_terms)) return false;
  const allowed = capabilities[surface];
  if (intent.sort && !allowed.sorts.includes(intent.sort)) return false;
  return Object.entries(intent).every(([field, value]) =>
    ["recognized", "explanation", "sort"].includes(field) || allowed.fields.includes(field)
    || value == null || value === false || (Array.isArray(value) && !value.length));
}

export function normalizeFilterText(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

export function matchesQuestionToken(id: string, label: string, token: string): boolean {
  const normalize = (value: string) => normalizeFilterText(value).replace(/^(?:q\s*|第\s*)/, "").replace(/\s*题$/, "");
  return [id, label].some((value) => normalize(value) === normalize(token));
}

export function normalizeFilterType(value: string): string {
  const aliases: Record<string, string> = {
    "计算题": "calculation", "计算": "calculation", "编程题": "programming", "编程": "programming", "coding": "programming",
    "证明题": "proof", "证明": "proof", "概念题": "concept", "选择题": "choice", "填空题": "fill_blank", "简答题": "short_answer", "问答题": "short_answer",
  };
  const key = normalizeFilterText(value);
  return aliases[key] ?? key;
}

/** Only whole instructions are presets. Unknown/compound wording goes to the interpreter. */
export function parseLocalTaskFilter(raw: string, surface: FilterIntentSurface): FilterIntentResult | null {
  const query = normalizeFilterText(raw);
  if (!query || /^(?:全部|显示全部|查看全部|清除筛选|show all|all records|clear filters)$/.test(query)) return { ...EMPTY_FILTER_INTENT };
  let result: FilterIntentResult | null = null;
  const match = query.match(/^(?:请)?(?:按|sort\s+(?:by\s+)?)?\s*(满分|maximum score|max score|题号|question(?: number)?|题型|type|姓名|name|学号|student id|id|覆盖率|coverage|待复核数|复核数|review count|置信度|confidence|得分率|score)\s*(升序|降序|从低到高|从高到低|从少到多|从多到少|asc(?:ending)?|desc(?:ending)?)(?:排列|排序)?$/);
  if (match) {
    const field = /满分|max/.test(match[1]) ? "max_score" : /题号|question/.test(match[1]) ? "question"
      : /题型|type/.test(match[1]) ? "type" : /姓名|name/.test(match[1]) ? "name"
        : /学号|\bid\b/.test(match[1]) ? "id" : /覆盖率|coverage/.test(match[1]) ? "coverage"
          : /复核|review/.test(match[1]) ? "review" : /置信度|confidence/.test(match[1]) ? "confidence" : "score";
    const direction = /降序|从高到低|从多到少|desc/.test(match[2]) ? "desc" : "asc";
    const sort = (field === "question" && direction === "asc" ? "question" : `${field}_${direction}`) as FilterIntentResult["sort"];
    result = { ...EMPTY_FILTER_INTENT, sort };
  } else if (/^(?:q\s*\d+(?:[._-]\d+)*|第\s*\d+(?:[._-]\d+)*\s*题)$/i.test(query)) {
    result = { ...EMPTY_FILTER_INTENT, question_tokens: [query] };
  } else if (["calculation", "programming", "proof", "concept", "choice", "fill_blank", "short_answer"].includes(normalizeFilterType(query))) {
    result = { ...EMPTY_FILTER_INTENT, question_types: [normalizeFilterType(query)] };
  } else if (surface === "question_preparation") {
    const states: Record<string, NonNullable<FilterIntentResult["preparation_status"]>> = {
      "低置信": "low_confidence", "低置信度": "low_confidence", "low confidence": "low_confidence", "low-confidence": "low_confidence",
      "冲突": "source_conflict", "来源冲突": "source_conflict", "conflict": "source_conflict", "解析异常": "parse_anomaly",
      "待关注": "attention", "待复核": "attention", "已准备": "ready",
    };
    const missing = query.match(/^(?:缺(?:少|失)?|missing\s+)(标答|答案|reference answer|answer|评分标准|rubric|测试样例|tests|题干|stem)$/);
    if (states[query]) result = { ...EMPTY_FILTER_INTENT, preparation_status: states[query] };
    if (missing) {
      const field = /标答|答案|answer/.test(missing[1]) ? "answer" : /评分标准|rubric/.test(missing[1]) ? "rubric" : /测试|tests/.test(missing[1]) ? "tests" : "stem";
      result = { ...EMPTY_FILTER_INTENT, material_field: field, material_status: "missing" };
    }
  } else if (surface === "submission_review" || surface === "student_answer_review") {
    const states: Record<string, NonNullable<FilterIntentResult["submission_status"]>> = {
      "缺答": "missing", "缺失": "missing", "未作答": "missing", "missing": "missing",
      "待复核": "review", "review": "review", "身份异常": "identity",
      "已识别": "recognized", "recognized": "recognized", "已校对": "reviewed", "reviewed": "reviewed",
    };
    if (states[query]) result = { ...EMPTY_FILTER_INTENT, submission_status: states[query] };
  }
  return result && supportsFilterIntent(result, surface) ? result : null;
}

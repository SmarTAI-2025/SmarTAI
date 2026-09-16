import type { FilterIntentResult, ProblemInfo, StudentSubmission } from "@/types";
import { questionSearchAliases } from "@/lib/questionSearch";
import { answerMap, getAnswerState, type SubmissionQuestion } from "@/lib/submissionReview";
import { compareSortableValues } from "@/components/ui/SortableTableHead";

export function emptyFilterIntent(): FilterIntentResult {
  return { recognized: true, min_score_percent: null, max_score_percent: null, pass_status: null, low_confidence: false, review_status: null, disagreement: false, annotated: false, sort: null, question_tokens: [], text_terms: [], explanation: "" };
}

/** Exact presets only: a partially understood sentence must go to the model. */
export function parsePreparationQuery(raw: string, surface: "question_preparation" | "submission_review" | "student_answer_review", literals: string[] = []): FilterIntentResult | null {
  const query = raw.trim().toLowerCase();
  const result = emptyFilterIntent();
  if (!query) return result;
  const sortMatch = query.match(/^(?:请)?(?:按|sort by\s+)?(满分|max(?:imum)? score|题号|question|题型|type|姓名|name|学号|id|覆盖率|coverage|待复核数|风险数|attention)(?:\s*)(升序|降序|从低到高|从高到低|ascending|descending|asc|desc)(?:排列|排序)?$/);
  if (sortMatch) {
    const key = /满分|max/.test(sortMatch[1]) ? "max_score" : /题号|question/.test(sortMatch[1]) ? "question" : /题型|type/.test(sortMatch[1]) ? "type" : /姓名|name/.test(sortMatch[1]) ? "name" : /学号|id/.test(sortMatch[1]) ? "id" : /覆盖率|coverage/.test(sortMatch[1]) ? "coverage" : "review";
    if (
      surface === "question_preparation"
        ? ["name", "id", "coverage"].includes(key)
        : surface === "submission_review"
          ? ["max_score", "type"].includes(key)
          : key !== "question"
    ) return null;
    const direction = /降序|从高到低|descending|desc/.test(sortMatch[2]) ? "desc" : "asc";
    result.sort = (key === "question" && direction === "asc" ? "question" : `${key}_${direction}`) as FilterIntentResult["sort"];
    return result;
  }
  if (/^(?:q\s*\d+(?:\.\d+)*|第\s*\d+(?:\.\d+)*\s*题)$/i.test(query)) {
    result.question_tokens = [query];
    return result;
  }
  if (surface === "question_preparation") {
    const types: Record<string, string> = { "计算题": "calculation", "calculation": "calculation", "编程题": "programming", "programming": "programming", "证明题": "proof", "选择题": "choice", "填空题": "fill_blank", "简答题": "short_answer", "概念题": "concept" };
    if (types[query]) { result.question_types = [types[query]]; return result; }
    const risks: Record<string, NonNullable<FilterIntentResult["preparation_status"]>> = { "低置信": "low_confidence", "low confidence": "low_confidence", "冲突": "source_conflict", "来源冲突": "source_conflict", "解析异常": "parse_anomaly", "待关注": "attention", "待复核": "attention", "已准备": "ready" };
    if (risks[query]) { result.preparation_status = risks[query]; return result; }
  } else {
    const states: Record<string, NonNullable<FilterIntentResult["submission_status"]>> = { "缺答": "missing", "未作答": "missing", "missing": "missing", "待复核": "review", "低置信": "review", "身份异常": "identity", "已识别": "recognized", "已校对": "reviewed" };
    if (states[query]) { result.submission_status = states[query]; return result; }
  }
  if (literals.some((value) => value.trim().toLowerCase() === query)) { result.text_terms = [raw.trim()]; return result; }
  return null;
}

export function questionTokenMatches(id: string, label: string, token: string) {
  const normalize = (value: string) => value.toLowerCase().trim().replace(/^(?:q\s*|第\s*)/, "").replace(/\s*题$/, "");
  return [id, label].some((value) => normalize(value) === normalize(token));
}

export function materialValue(problem: ProblemInfo, field: "stem" | "answer" | "rubric" | "tests") {
  if (field === "tests" && !/program|code|编程/i.test(problem.type)) return null;
  const ready = field === "stem" ? !!problem.stem?.trim() : field === "answer" ? !!problem.reference_answer?.trim() : field === "rubric" ? !!problem.criterion?.trim() : !!problem.test_cases?.length;
  if (!ready) return "missing";
  if (field === "stem") return "recognized";
  const key = field === "answer" ? "reference_answer" : field === "rubric" ? "criterion" : "test_cases";
  if (problem.material_provenance?.[key]) return "recognized";
  if (problem.ai_completion_provenance?.[key] || (field === "tests" && problem.test_cases?.every((test) => test.source === "llm_generated"))) return "generated";
  return "ready";
}

export function selectPreparationProblems(problems: ProblemInfo[], intent: FilterIntentResult | null) {
  if (!intent?.recognized) return problems;
  const selected = problems.filter((problem) => {
    const issues = (problem.preparation_issues ?? []).filter((issue) => issue.status === "open");
    if (intent.question_tokens.length && !intent.question_tokens.some((token) => questionTokenMatches(problem.q_id, problem.number || problem.q_id, token))) return false;
    const type = `${problem.type} ${questionSearchAliases(problem.type || "")}`.toLowerCase();
    if (intent.question_types?.length && !intent.question_types.some((term) => type.includes(term.toLowerCase()))) return false;
    const points = problem.max_score;
    if (intent.min_max_score != null && (points == null || points < intent.min_max_score)) return false;
    if (intent.max_max_score != null && (points == null || points > intent.max_max_score)) return false;
    const state = intent.low_confidence ? "low_confidence" : intent.preparation_status;
    if (state === "attention" && !issues.length) return false;
    if (state === "ready" && issues.length) return false;
    if (state === "low_confidence" && !issues.some((issue) => issue.code === "low_confidence")) return false;
    if (state === "source_conflict" && !issues.some((issue) => /conflict/.test(issue.code))) return false;
    if (state === "parse_anomaly" && !issues.some((issue) => /parse_anomaly|generation_failed|invalid_test_case|reference_solution_failed_case/.test(issue.code))) return false;
    if (intent.material_field && intent.material_status) {
      const value = materialValue(problem, intent.material_field);
      if (intent.material_status === "ready" ? value == null || value === "missing" : value !== intent.material_status) return false;
    }
    const text = `${problem.number} ${problem.q_id} ${type} ${problem.stem ?? ""} ${problem.reference_answer ?? ""} ${problem.criterion ?? ""}`.toLowerCase();
    return intent.text_terms.every((term) => text.includes(term.toLowerCase()));
  });
  const sort = intent.sort;
  if (!sort) return selected;
  const direction = sort.endsWith("_desc") ? "desc" : "asc";
  const value = (p: ProblemInfo) => sort.startsWith("max_score") ? p.max_score : sort.startsWith("type") ? p.type : sort.startsWith("review") ? (p.preparation_issues ?? []).filter((issue) => issue.status === "open").length : p.number || p.q_id;
  return [...selected].sort((a, b) => compareSortableValues(value(a), value(b), direction));
}

export function submissionStateMatches(student: StudentSubmission, questions: SubmissionQuestion[], status: FilterIntentResult["submission_status"]) {
  if (!status) return true;
  if (status === "identity") return student.identity_status === "needs_review";
  const answers = answerMap(student);
  const states = questions.map((q) => getAnswerState(answers.get(q.id)));
  return states.some((state) => status === "missing" ? state === "missing" || state === "empty" : status === "review" ? !["recognized", "reviewed"].includes(state) : state === status);
}

export function selectSubmissionIntent(students: StudentSubmission[], questions: SubmissionQuestion[], intent: FilterIntentResult | null) {
  if (!intent?.recognized) return { students, questions };
  const selectedQuestions = questions.filter((q) => !intent.question_tokens.length || intent.question_tokens.some((token) => questionTokenMatches(q.id, q.label, token)));
  const selected = students.filter((student) => submissionStateMatches(student, selectedQuestions, intent.submission_status)
    && intent.text_terms.every((term) => `${student.stu_id} ${student.stu_name}`.toLowerCase().includes(term.toLowerCase())));
  const sort = intent.sort;
  const value = (student: StudentSubmission) => sort?.startsWith("name") ? student.stu_name || student.stu_id
    : sort?.startsWith("coverage") ? selectedQuestions.filter((q) => !!answerMap(student).get(q.id)?.content?.trim()).length
      : sort?.startsWith("review") ? selectedQuestions.filter((q) => !["recognized", "reviewed"].includes(getAnswerState(answerMap(student).get(q.id)))).length : student.stu_id;
  return { students: sort ? [...selected].sort((a, b) => compareSortableValues(value(a), value(b), sort.endsWith("_desc") ? "desc" : "asc")) : selected, questions: intent.sort === "question_desc" ? [...selectedQuestions].reverse() : selectedQuestions };
}

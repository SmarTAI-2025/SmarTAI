import type { FilterIntentResult, ProblemInfo, StudentSubmission } from "@/types";
import { isProgrammingProblem } from "@/lib/questionPreparation";
import { questionSearchAliases } from "@/lib/questionSearch";
import { answerMap, getAnswerState, type SubmissionQuestion, type SubmissionReviewFilter, type SubmissionReviewSelection } from "@/lib/submissionReview";
import { compareValues } from "@/lib/sortValues";
import { EMPTY_FILTER_INTENT, matchesQuestionToken, normalizeFilterText, normalizeFilterType, parseLocalTaskFilter } from "@/lib/taskFilterIntent";

function includesText(source: string, term: string) {
  return normalizeFilterText(`${source} ${questionSearchAliases(source)}`).includes(normalizeFilterText(term));
}
function problemText(problem: ProblemInfo) {
  return [problem.number, problem.q_id, problem.type, problem.stem, problem.reference_answer, problem.criterion, problem.solution_code].filter(Boolean).join(" ");
}

export function resolvePreparationQuery(problems: ProblemInfo[], raw: string): FilterIntentResult | null {
  const preset = parseLocalTaskFilter(raw, "question_preparation");
  if (preset) return preset;
  return raw.trim() && problems.some((problem) => includesText(problemText(problem), raw))
    ? { ...EMPTY_FILTER_INTENT, text_terms: [raw.trim()] } : null;
}

export function materialStatus(problem: ProblemInfo, field: NonNullable<FilterIntentResult["material_field"]>) {
  if (field === "tests" && !isProgrammingProblem(problem)) return null;
  const present = field === "stem" ? !!problem.stem?.trim() : field === "answer" ? !!problem.reference_answer?.trim()
    : field === "rubric" ? !!problem.criterion?.trim() : !!problem.test_cases?.length;
  if (!present) return "missing";
  if (field === "stem") return "recognized";
  const key = field === "answer" ? "reference_answer" : field === "rubric" ? "criterion" : "test_cases";
  if (problem.material_provenance?.[key]) return "recognized";
  if (problem.ai_completion_provenance?.[key] || (field === "tests" && problem.test_cases?.every((item) => item.source === "llm_generated"))) return "generated";
  return "ready";
}

export function selectPreparationQuestions(problems: ProblemInfo[], intent: FilterIntentResult | null): ProblemInfo[] {
  if (!intent?.recognized) return problems;
  const selected = problems.filter((problem) => {
    const issues = (problem.preparation_issues ?? []).filter((issue) => issue.status === "open");
    if (intent.question_tokens.length && !intent.question_tokens.some((token) => matchesQuestionToken(problem.q_id, problem.number || problem.q_id, token))) return false;
    if (intent.question_types?.length && !intent.question_types.some((type) => normalizeFilterType(type) === normalizeFilterType(problem.type || ""))) return false;
    if (intent.min_max_score != null && (problem.max_score == null || problem.max_score < intent.min_max_score)) return false;
    if (intent.max_max_score != null && (problem.max_score == null || problem.max_score > intent.max_max_score)) return false;
    if ((intent.low_confidence || intent.preparation_status === "low_confidence") && !issues.some((issue) => issue.code === "low_confidence")) return false;
    if (intent.preparation_status === "attention" && !issues.length) return false;
    if (intent.preparation_status === "ready" && issues.length) return false;
    if (intent.preparation_status === "source_conflict" && !issues.some((issue) => issue.code.includes("conflict"))) return false;
    if (intent.preparation_status === "parse_anomaly" && !issues.some((issue) => ["parse_anomaly", "generation_failed", "invalid_test_case", "reference_solution_failed_case"].includes(issue.code))) return false;
    if (intent.material_field && intent.material_status) {
      const status = materialStatus(problem, intent.material_field);
      if (intent.material_status === "ready" ? status == null || status === "missing" : status !== intent.material_status) return false;
    }
    return intent.text_terms.every((term) => includesText(problemText(problem), term));
  });
  if (!intent.sort) return selected;
  const sort = intent.sort;
  const value = (problem: ProblemInfo) => sort.startsWith("max_score") ? problem.max_score
    : sort.startsWith("type") ? normalizeFilterType(problem.type || "")
      : sort.startsWith("review") ? (problem.preparation_issues ?? []).filter((issue) => issue.status === "open").length
        : problem.number || problem.q_id;
  return selected.sort((a, b) => compareValues(value(a), value(b), sort.endsWith("_desc") ? "desc" : "asc"));
}

export function resolveSubmissionQuery(students: StudentSubmission[], questions: SubmissionQuestion[], raw: string, student?: StudentSubmission): FilterIntentResult | null {
  const preset = parseLocalTaskFilter(raw, student ? "student_answer_review" : "submission_review");
  if (preset) return preset;
  if (!raw.trim()) return { ...EMPTY_FILTER_INTENT };
  const answers = student ? answerMap(student) : null;
  const matchedQuestions = questions.filter((question) => includesText(`${question.id} ${question.label} ${question.type} ${question.stem} ${answers?.get(question.id)?.content ?? ""}`, raw));
  if (matchedQuestions.length) return { ...EMPTY_FILTER_INTENT, question_tokens: matchedQuestions.map((question) => question.id) };
  if (!student && students.some((item) => includesText(`${item.stu_id} ${item.stu_name}`, raw))) return { ...EMPTY_FILTER_INTENT, text_terms: [raw.trim()] };
  return null;
}

function questionMatches(question: SubmissionQuestion, intent: FilterIntentResult) {
  return (!intent.question_tokens.length || intent.question_tokens.some((token) => matchesQuestionToken(question.id, question.label, token)))
    && (!intent.question_types?.length || intent.question_types.some((type) => normalizeFilterType(type) === normalizeFilterType(question.type)));
}
function answerMatches(state: ReturnType<typeof getAnswerState>, status: FilterIntentResult["submission_status"]) {
  if (status === "missing") return state === "missing" || state === "empty";
  if (status === "review") return !["recognized", "reviewed"].includes(state);
  return status !== "recognized" && status !== "reviewed" || state === status;
}

export function selectStudentAnswerQuestions(questions: SubmissionQuestion[], student: StudentSubmission | undefined, intent: FilterIntentResult | null): SubmissionQuestion[] {
  if (!intent?.recognized) return questions;
  if (!student || (intent.submission_status === "identity" && student.identity_status !== "needs_review")) return [];
  const answers = answerMap(student);
  const selected = questions.filter((question) => questionMatches(question, intent)
    && answerMatches(getAnswerState(answers.get(question.id)), intent.submission_status)
    && intent.text_terms.every((term) => includesText(`${question.id} ${question.label} ${question.type} ${question.stem} ${answers.get(question.id)?.content ?? ""}`, term)));
  return intent.sort === "question_desc" ? selected.reverse() : selected;
}

export function selectSubmissionQuestions(students: StudentSubmission[], questions: SubmissionQuestion[], intent: FilterIntentResult | null, filter: SubmissionReviewFilter): SubmissionReviewSelection {
  const plan = intent?.recognized ? intent : EMPTY_FILTER_INTENT;
  const selectedQuestions = questions.filter((question) => questionMatches(question, plan));
  const records = students.map((student) => {
    const answers = answerMap(student);
    const states = selectedQuestions.map((question) => getAnswerState(answers.get(question.id)));
    return { student, states, coverage: selectedQuestions.filter((question) => !!answers.get(question.id)?.content?.trim()).length,
      review: states.filter((state) => answerMatches(state, "review")).length };
  }).filter(({ student, states }) => {
    if (!selectedQuestions.length) return false;
    if (!plan.text_terms.every((term) => includesText(`${student.stu_id} ${student.stu_name}`, term))) return false;
    return [plan.submission_status, filter === "all" ? null : filter].every((status) => {
      if (status === "identity") return student.identity_status === "needs_review";
      if (status === "reviewed") return states.length > 0 && states.every((state) => state === "reviewed");
      return !status || states.some((state) => answerMatches(state, status));
    });
  });
  const sort = plan.sort;
  if (sort) {
    const value = (record: typeof records[number]) => sort.startsWith("name") ? record.student.stu_name || record.student.stu_id
      : sort.startsWith("coverage") ? record.coverage : sort.startsWith("review") ? record.review : record.student.stu_id;
    records.sort((a, b) => compareValues(value(a), value(b), sort.endsWith("_desc") ? "desc" : "asc") || compareValues(a.student.stu_id, b.student.stu_id));
  }
  const status = filter !== "all" ? filter : plan.submission_status;
  return {
    students: records.map((record) => record.student), questions: selectedQuestions, confidenceAlias: false,
    explanation: !records.length ? "no_match" : status === "identity" || status === "missing" || status === "review" ? status
      : plan.question_tokens.length || plan.question_types?.length ? "question" : plan.text_terms.length ? "student" : "all",
  };
}

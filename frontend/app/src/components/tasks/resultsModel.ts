import type { Correction, ProblemInfo, StudentAnswerInfo, StudentResult, StudentSubmission, Task, TaskResultResponse } from "@/types";
import type { Locale } from "@/i18n/messages";

export const LOW_CONFIDENCE_THRESHOLD = 0.65;

export type CorrectionScoreSource =
  | "ai_untouched"
  | "teacher_confirmed_same"
  | "teacher_changed"
  | "hard_failure";

const HARD_FAILURE_METHODS = new Set(["all_failed", "quota_exhausted"]);
const HARD_FAILURE_REASONS = new Set([
  "llm_failed",
  "quota_exhausted",
  "invalid_score_scale",
  "missing_correction",
  "missing_student_result",
]);

export interface StudentSummary {
  id: string;
  name: string;
  corrections: Correction[];
  answers: StudentAnswerInfo[];
  answerByQuestion: Map<string, StudentAnswerInfo>;
  totalScore: number;
  totalMax: number;
  percent: number | null;
  avgConfidence: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
}

export interface ReviewScoreSourceSummary {
  aiUntouched: number;
  teacherConfirmedSame: number;
  teacherChanged: number;
  hardFailure: number;
  total: number;
}

export interface QuestionEntry {
  student: StudentSummary;
  correction: Correction;
  answer?: StudentAnswerInfo;
}

export interface QuestionSummary {
  id: string;
  problem?: ProblemInfo;
  label: string;
  type?: string;
  stem?: string;
  criterion?: string;
  entries: QuestionEntry[];
  count: number;
  avgScore: number | null;
  maxScore: number;
  avgPercent: number | null;
  minScore: number | null;
  maxObservedScore: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
}

export interface ResultsModel {
  task?: Task;
  result?: TaskResultResponse;
  problems: ProblemInfo[];
  students: StudentSummary[];
  questions: QuestionSummary[];
  classAverageScore: number | null;
  classAverageMax: number | null;
  classAveragePercent: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
  timestamp?: number;
}

export function buildResultsModel(task?: Task, result?: TaskResultResponse): ResultsModel {
  const rawProblems = result?.problem_data ?? task?.problem_data ?? {};
  const rawStudentData = result?.student_data ?? task?.student_data ?? {};
  const problems = Object.values(rawProblems).sort(compareProblems);
  const problemOrder = new Map(problems.map((problem, index) => [problem.q_id, index]));
  const submissions = Object.values(rawStudentData);
  const submissionById = new Map(submissions.map((submission) => [submission.stu_id, submission]));
  const students = (result?.results ?? []).map((studentResult) =>
    buildStudentSummary(studentResult, submissionById.get(studentResult.student_id), problemOrder),
  );

  students.sort((a, b) => compareStudentNames(a.name, b.name) || a.id.localeCompare(b.id, undefined, { numeric: true }));

  const qIds = new Set(problems.map((problem) => problem.q_id));
  for (const student of students) {
    for (const correction of student.corrections) {
      qIds.add(correction.q_id);
    }
  }

  const entriesByQuestion = new Map<string, QuestionEntry[]>();
  for (const student of students) {
    for (const correction of student.corrections) {
      const entries = entriesByQuestion.get(correction.q_id) ?? [];
      entries.push({
        student,
        correction,
        answer: student.answerByQuestion.get(correction.q_id),
      });
      entriesByQuestion.set(correction.q_id, entries);
    }
  }

  const questions = Array.from(qIds)
    .sort((a, b) => compareQuestionIds(a, b, problemOrder))
    .map((qId) => buildQuestionSummary(qId, rawProblems[qId], entriesByQuestion.get(qId) ?? []));

  const studentsWithResolvedScores = students.filter((student) => student.totalMax > 0);
  const classAverageScore = averageOrNull(studentsWithResolvedScores.map((student) => student.totalScore));
  const classAverageMax = averageOrNull(studentsWithResolvedScores.map((student) => student.totalMax));
  const classAveragePercent = averageOrNull(students.map((student) => student.percent));
  const lowConfidenceCount = students.reduce((sum, student) => sum + student.lowConfidenceCount, 0);
  const reviewCount = students.reduce((sum, student) => sum + student.reviewCount, 0);

  return {
    task,
    result,
    problems,
    students,
    questions,
    classAverageScore,
    classAverageMax,
    classAveragePercent,
    lowConfidenceCount,
    reviewCount,
    timestamp: result?.timestamp,
  };
}

export function compareProblems(a: ProblemInfo, b: ProblemInfo) {
  return compareProblemLabels(problemLabel(a), problemLabel(b)) || a.q_id.localeCompare(b.q_id, undefined, { numeric: true });
}

export function problemLabel(problem?: Pick<ProblemInfo, "q_id" | "number"> | null, fallbackId?: string) {
  const number = String(problem?.number ?? "").trim();
  if (number) {
    return `Q${number}`;
  }
  return String(problem?.q_id ?? fallbackId ?? "题目");
}

export function formatScore(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(1);
}

export function aiCorrectionScore(correction: Correction): number | null {
  if (hasHardFailureSignal(correction)) return null;
  if (isFiniteScore(correction.provisional_score)) return correction.provisional_score;
  if (!isFiniteScore(correction.teacher_score) && isFiniteScore(correction.score)) return correction.score;
  return null;
}

export function effectiveCorrectionScore(correction: Correction): number | null {
  if (isFiniteScore(correction.teacher_score)) return correction.teacher_score;
  if (hasHardFailureSignal(correction)) return null;
  if (isFiniteScore(correction.score)) return correction.score;
  return isFiniteScore(correction.provisional_score) ? correction.provisional_score : null;
}

export function correctionScoreSource(correction: Correction): CorrectionScoreSource {
  if (isFiniteScore(correction.teacher_score)) {
    const aiScore = isFiniteScore(correction.provisional_score) ? correction.provisional_score : null;
    return aiScore !== null && Math.abs(correction.teacher_score - aiScore) < 1e-9
      ? "teacher_confirmed_same"
      : "teacher_changed";
  }
  return effectiveCorrectionScore(correction) === null ? "hard_failure" : "ai_untouched";
}

export function formatCorrectionScoreSource(correction: Correction, locale: Locale): string {
  const labels: Record<CorrectionScoreSource, [string, string]> = {
    ai_untouched: ["采用 AI 分数 · 教师未操作", "AI score used · no teacher action"],
    teacher_confirmed_same: ["教师已处理 · 沿用 AI 分数", "Teacher handled · AI score retained"],
    teacher_changed: ["教师已修改分数", "Teacher changed the score"],
    hard_failure: ["无法给出分数 · 需要处理", "No valid score · action required"],
  };
  return labels[correctionScoreSource(correction)][locale === "en-US" ? 1 : 0];
}

export function summarizeReviewScoreSources(corrections: Correction[]): ReviewScoreSourceSummary {
  const summary: ReviewScoreSourceSummary = {
    aiUntouched: 0,
    teacherConfirmedSame: 0,
    teacherChanged: 0,
    hardFailure: 0,
    total: 0,
  };
  for (const correction of corrections) {
    if (!hasReviewSignal(correction)) continue;
    summary.total += 1;
    const source = correctionScoreSource(correction);
    if (source === "ai_untouched") summary.aiUntouched += 1;
    else if (source === "teacher_confirmed_same") summary.teacherConfirmedSame += 1;
    else if (source === "teacher_changed") summary.teacherChanged += 1;
    else summary.hardFailure += 1;
  }
  return summary;
}

export function shouldHideAutomatedScores(correction: Correction): boolean {
  return hasReviewSignal(correction) && correctionScoreSource(correction) === "ai_untouched";
}

/** Review surfaces hide an untouched AI score while formal result math still uses it. */
export function displayableCorrectionScore(correction: Correction): number | null {
  if (isFiniteScore(correction.teacher_score)) {
    return correction.teacher_score;
  }
  if (shouldHideAutomatedScores(correction)) {
    return null;
  }
  return effectiveCorrectionScore(correction);
}

export function correctionReviewDraftScore(correction: Correction): string {
  const score = displayableCorrectionScore(correction);
  return score === null ? "" : String(score);
}

export function reviewConfirmationScore(correction: Correction, draftScore: string): number | null {
  const normalized = draftScore.trim();
  if (normalized) {
    const score = Number(normalized);
    return Number.isFinite(score) ? score : null;
  }
  return aiCorrectionScore(correction);
}

export function correctionReviewReasonIds(correction: Correction): string[] {
  return Array.from(new Set(
    [...(correction.review_reasons ?? []), ...(correction.initial_review_reasons ?? [])]
      .flatMap((reason) => reason.split(","))
      .map((reason) => reason.trim())
      .filter(Boolean),
  ));
}

export function formatPercent(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  return `${Math.round(value)}%`;
}

export function formatConfidence(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const percent = value <= 1 ? value * 100 : value;
  return `${Math.round(percent)}%`;
}

export function isLowConfidence(correction: Correction) {
  return safeNumber(correction.confidence) < LOW_CONFIDENCE_THRESHOLD;
}

export function hasReviewSignal(correction: Correction) {
  return correction.requires_human_review
    || isLowConfidence(correction)
    || correctionReviewReasonIds(correction).length > 0
    || HARD_FAILURE_METHODS.has(correction.synthesis_method ?? "");
}

export function reviewReasonLabel(reason: string) {
  const labels: Record<string, string> = {
    high_indecisiveness: "专家分歧较大",
    low_confidence: "置信度偏低",
    score_spread_high: "评分差异较大",
    parse_failed: "解析失败",
    transient_llm: "模型临时错误",
    quota_exhausted: "额度不足",
    general: "需要人工确认",
  };
  return labels[reason] ?? reason.replaceAll("_", " ");
}

export function clampPercent(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, value));
}

function buildStudentSummary(
  result: StudentResult,
  submission: StudentSubmission | undefined,
  problemOrder: Map<string, number>,
): StudentSummary {
  const corrections = [...(result.corrections ?? [])].sort((a, b) => compareQuestionIds(a.q_id, b.q_id, problemOrder));
  const answers = result.student_answers?.length ? result.student_answers : (submission?.stu_ans ?? []);
  const answerByQuestion = new Map(answers.map((answer) => [answer.q_id, answer]));
  const resolvedScores = corrections.flatMap((correction) => {
    const score = effectiveCorrectionScore(correction);
    const maxScore = safeNumber(correction.max_score);
    return score === null || maxScore <= 0 ? [] : [{ score, maxScore }];
  });
  const totalScore = sum(resolvedScores.map((item) => item.score));
  const totalMax = sum(resolvedScores.map((item) => item.maxScore));
  const confidenceValues = corrections.map((correction) => safeNumber(correction.confidence));
  const lowConfidenceCount = corrections.filter(isLowConfidence).length;
  const reviewCount = corrections.filter((correction) => correction.requires_human_review).length;

  return {
    id: result.student_id,
    name: String(result.student_name || submission?.stu_name || result.student_id),
    corrections,
    answers,
    answerByQuestion,
    totalScore,
    totalMax,
    percent: totalMax > 0 ? (totalScore / totalMax) * 100 : null,
    avgConfidence: confidenceValues.length ? average(confidenceValues) : null,
    lowConfidenceCount,
    reviewCount,
  };
}

function buildQuestionSummary(qId: string, problem: ProblemInfo | undefined, entries: QuestionEntry[]): QuestionSummary {
  const scores = entries
    .map((entry) => effectiveCorrectionScore(entry.correction))
    .filter((value): value is number => value !== null);
  const maxScores = entries.map((entry) => safeNumber(entry.correction.max_score)).filter((value) => value > 0);
  const percents = entries
    .map((entry) => {
      const maxScore = safeNumber(entry.correction.max_score);
      const score = effectiveCorrectionScore(entry.correction);
      return score !== null && maxScore > 0 ? (score / maxScore) * 100 : null;
    })
    .filter((value): value is number => value !== null);

  return {
    id: qId,
    problem,
    label: problemLabel(problem, qId),
    type: problem?.type,
    stem: problem?.stem,
    criterion: problem?.criterion,
    entries,
    count: entries.length,
    avgScore: averageOrNull(scores),
    maxScore: maxScores.length ? Math.max(...maxScores) : 0,
    avgPercent: percents.length ? average(percents) : null,
    minScore: scores.length ? Math.min(...scores) : null,
    maxObservedScore: scores.length ? Math.max(...scores) : null,
    lowConfidenceCount: entries.filter((entry) => isLowConfidence(entry.correction)).length,
    reviewCount: entries.filter((entry) => entry.correction.requires_human_review).length,
  };
}

function compareQuestionIds(a: string, b: string, problemOrder: Map<string, number>) {
  const aOrder = problemOrder.get(a);
  const bOrder = problemOrder.get(b);
  if (aOrder !== undefined && bOrder !== undefined) {
    return aOrder - bOrder;
  }
  if (aOrder !== undefined) {
    return -1;
  }
  if (bOrder !== undefined) {
    return 1;
  }
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function compareProblemLabels(a: string, b: string) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function compareStudentNames(a: string, b: string) {
  return a.localeCompare(b, "zh-Hans-CN", { numeric: true, sensitivity: "base" });
}

function sum(values: number[]) {
  return values.reduce((total, value) => total + value, 0);
}

function average(values: number[]) {
  return values.length ? sum(values) / values.length : 0;
}

function averageOrNull(values: Array<number | null>) {
  const clean = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return clean.length ? average(clean) : null;
}

function safeNumber(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function isFiniteScore(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function hasHardFailureSignal(correction: Correction): boolean {
  if (isFiniteScore(correction.teacher_score)) return false;
  if (HARD_FAILURE_METHODS.has(correction.synthesis_method ?? "")) return true;
  return correctionReviewReasonIds(correction).some((reason) => HARD_FAILURE_REASONS.has(reason));
}

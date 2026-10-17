import type { Correction, ProblemInfo, StudentAnswerInfo, StudentResult, StudentSubmission, Task, TaskResultResponse } from "@/types";
import type { Locale } from "@/i18n/messages";
import type { ReviewAttention } from "@/lib/reviewConfirmation";
import { confidenceNeedsAttention, confidenceRequiresReview, confidenceValue } from "@/lib/confidencePolicy";
export { LOW_CONFIDENCE_THRESHOLD } from "@/lib/confidencePolicy";

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
    return `Q${number.replace(/^q\s*/i, "")}`;
  }
  return String(problem?.q_id ?? fallbackId ?? "题目");
}

function roundAIScore(value: number, maxScore: number): number {
  const [coefficient, exponent = "0"] = String(value).split("e");
  const shifted = Number(`${coefficient}e${Number(exponent) + 1}`);
  const rounded = Number.isFinite(shifted) ? Math.round(shifted) / 10 : value;
  return value <= maxScore ? Math.min(rounded, maxScore) : rounded;
}

export function formatScore(value: number | null | undefined, maxScore?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  if (value === maxScore) return String(value);
  return roundAIScore(value, Number.POSITIVE_INFINITY).toFixed(1);
}

export function aiCorrectionScore(correction: Correction): number | null {
  if (hasHardFailureSignal(correction)) return null;
  if (isFiniteScore(correction.provisional_score)) return roundAIScore(correction.provisional_score, correction.max_score);
  if (!isFiniteScore(correction.teacher_score) && isFiniteScore(correction.score)) return roundAIScore(correction.score, correction.max_score);
  return null;
}

export function effectiveCorrectionScore(correction: Correction): number | null {
  if (isFiniteScore(correction.teacher_score)) return correction.teacher_score;
  if (hasHardFailureSignal(correction)) return null;
  if (isFiniteScore(correction.score)) return roundAIScore(correction.score, correction.max_score);
  return aiCorrectionScore(correction);
}

export function correctionScoreSource(correction: Correction): CorrectionScoreSource {
  if (isFiniteScore(correction.teacher_score)) {
    const aiScore = aiCorrectionScore(correction);
    return aiScore !== null && Math.abs(correction.teacher_score - aiScore) < 1e-9
      ? "teacher_confirmed_same"
      : "teacher_changed";
  }
  return effectiveCorrectionScore(correction) === null ? "hard_failure" : "ai_untouched";
}

export function formatCorrectionScoreSource(correction: Correction, locale: Locale): string {
  if (hasReviewSignal(correction)) {
    if (effectiveCorrectionScore(correction) === null) return locale === "en-US" ? "No valid score · action required" : "无法给出分数 · 需要处理";
    return locale === "en-US" ? "Review pending" : "待复核";
  }
  if (!isCorrectionReviewConfirmed(correction)) return locale === "en-US" ? "Current score" : "当前采用分数";
  return correctionScoreSource(correction) === "teacher_changed"
    ? locale === "en-US" ? "Teacher changed the score" : "教师已修改分数"
    : locale === "en-US" ? "Teacher confirmed" : "教师已确认";
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
    if (!hasReviewSignal(correction) && !isCorrectionReviewConfirmed(correction)) continue;
    summary.total += 1;
    const source = hasReviewSignal(correction)
      ? effectiveCorrectionScore(correction) === null ? "hard_failure" : "ai_untouched"
      : correctionScoreSource(correction);
    if (source === "ai_untouched") summary.aiUntouched += 1;
    else if (source === "teacher_confirmed_same") summary.teacherConfirmedSame += 1;
    else if (source === "teacher_changed") summary.teacherChanged += 1;
    else summary.hardFailure += 1;
  }
  return summary;
}

export function correctionReviewDraftScore(correction: Correction): string {
  const score = effectiveCorrectionScore(correction);
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
  const confidence = confidenceValue(value);
  return confidence === null ? "--" : `${Math.round(confidence * 100)}%`;
}

export function isLowConfidence(correction: Correction) {
  return !isCorrectionReviewConfirmed(correction) && confidenceNeedsAttention(correction.confidence);
}

export function hasReviewSignal(correction: Correction) {
  return correctionReviewAttention(correction) !== "none";
}

export function isCorrectionReviewConfirmed(correction: Correction): boolean {
  return Number.isFinite(correction.teacher_score) && correction.review_status === "confirmed" && !correctionReviewBlocked(correction);
}

function validReviewScore(value: number | null | undefined, maxScore: number): boolean {
  return typeof value === "number" && Number.isFinite(value) && Number.isFinite(maxScore) && maxScore > 0 && value >= 0 && value <= maxScore;
}

export function correctionReviewBlocked(correction: Correction): boolean {
  if (!validReviewScore(effectiveCorrectionScore(correction), correction.max_score)) return true;
  const originalScore = correction.provisional_score === undefined ? correction.score : correction.provisional_score;
  return !validReviewScore(originalScore, correction.max_score) && correction.review_status !== "confirmed";
}

/** One pending-review rule for the review queue, analysis, filters and charts. */
export function correctionReviewAttention(correction: Correction): ReviewAttention {
  return correctionReviewNextBlocked(correction) ? "blocking" : "none";
}

/** A valid score can be confirmed even when continuing requires that confirmation. */
export function correctionReviewNextBlocked(correction: Correction): boolean {
  return correctionReviewBlocked(correction) || (!isCorrectionReviewConfirmed(correction)
    && (confidenceRequiresReview(correction.confidence)
      || correctionReviewReasonIds(correction).includes("grading_paused")));
}

export function getExpertScoreSpread(correction: Correction): number {
  const scores = correction.expert_results?.filter(expert => !expert.error_kind)
    .map(expert => expert.score)
    .filter((score): score is number => typeof score === "number" && Number.isFinite(score)) ?? [];
  return scores.length ? Math.max(...scores) - Math.min(...scores) : 0;
}

export function hasExpertDisagreement(correction: Correction): boolean {
  return correctionReviewReasonIds(correction).some(reason => reason === "high_indecisiveness" || reason === "score_spread_high")
    || getExpertScoreSpread(correction) > Math.max(1, correction.max_score * 0.25);
}

export function reviewReasonLabel(reason: string, locale: Locale = "zh-CN") {
  const english: Record<string, string> = {
    grading_paused: "Paused before grading finished; check and confirm",
    high_indecisiveness: "Experts disagree", low_confidence: "Low confidence", score_spread_high: "Large score difference",
    minority_veto: "Outlying expert score", parse_failed: "Could not parse result", degraded_to_single: "Only one expert succeeded (low confidence)",
    transient_llm: "Model network error or timeout", llm_failed: "Model could not grade", quota_exhausted: "Quota exhausted", general: "Teacher review required",
  };
  if (locale === "en-US") return english[reason] ?? reason.replaceAll("_", " ");
  const labels: Record<string, string> = {
    grading_paused: "已暂停，批改未完成，请核对并确认",
    high_indecisiveness: "专家分歧较大",
    low_confidence: "置信度偏低",
    score_spread_high: "评分差异较大",
    parse_failed: "解析失败",
    degraded_to_single: "仅单专家成功（低置信度）",
    transient_llm: "模型网络或超时错误",
    llm_failed: "模型未完成批改",
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
  const confidenceValues = corrections.map((correction) => confidenceValue(correction.confidence)).filter((value): value is number => value !== null);
  const lowConfidenceCount = corrections.filter(isLowConfidence).length;
  const reviewCount = corrections.filter(hasReviewSignal).length;

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
    lowConfidenceCount: entries.filter(({ correction }) => isLowConfidence(correction)).length,
    reviewCount: entries.filter((entry) => hasReviewSignal(entry.correction)).length,
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
  if (HARD_FAILURE_METHODS.has(correction.synthesis_method ?? "")) return true;
  return correctionReviewReasonIds(correction).some((reason) => HARD_FAILURE_REASONS.has(reason));
}

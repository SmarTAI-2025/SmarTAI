import { describe, expect, it } from "vitest";
import type { Correction, FilterIntentResult } from "@/types";
import type { ResultsModel, StudentSummary } from "@/components/tasks/resultsModel";
import { collectResultReviewItems } from "@/components/tasks/resultsReviewModel";
import {
  reviewCellKey,
  reviewQueryNeedsIntentFallback,
  selectReviewOverview,
  selectReviewOverviewFromIntent,
} from "./reviewOverview";

function correction(qId: string, overrides: Partial<Correction> = {}): Correction {
  return {
    q_id: qId,
    type: "calculation",
    score: 8,
    max_score: 10,
    confidence: 0.9,
    comment: "",
    steps: [],
    expert_results: [],
    requires_human_review: false,
    review_reasons: [],
    ...overrides,
  };
}

function student(id: string, name: string, percent: number, corrections = [correction("Q1")]): StudentSummary {
  return {
    id,
    name,
    corrections,
    answers: [],
    answerByQuestion: new Map(),
    totalScore: percent / 10,
    totalMax: 10,
    percent,
    avgConfidence: 0.9,
    lowConfidenceCount: 0,
    reviewCount: 0,
  };
}

function questionSummary(id: string) {
  return {
    ...question,
    id,
    label: id,
  };
}

function intent(overrides: Partial<FilterIntentResult> = {}): FilterIntentResult {
  return {
    recognized: true,
    min_score_percent: null,
    max_score_percent: null,
    pass_status: null,
    low_confidence: false,
    review_status: null,
    disagreement: false,
    annotated: false,
    sort: null,
    question_tokens: [],
    text_terms: [],
    explanation: "",
    ...overrides,
  };
}

const low = student("student-low", "Low", 72);
const high = student("student-high", "High", 95);
const question = {
  id: "Q1",
  label: "Q1",
  entries: [],
  count: 2,
  avgScore: 8,
  maxScore: 10,
  avgPercent: 80,
  minScore: 7,
  maxObservedScore: 9,
  lowConfidenceCount: 0,
  reviewCount: 0,
};
const model = {
  problems: [],
  students: [low, high],
  questions: [question],
  classAverageScore: 8,
  classAverageMax: 10,
  classAveragePercent: 80,
  lowConfidenceCount: 0,
  reviewCount: 0,
} as ResultsModel;

describe("review overview smart filter", () => {
  it("treats 90分以下 as a total-score student filter", () => {
    const selection = selectReviewOverview(model, [], new Set(), "90分以下的学生");

    expect(selection.students.map((item) => item.id)).toEqual(["student-low"]);
    expect(selection.unresolvedText).toBe("");
  });

  it("sorts a bare 从高到低 request locally", () => {
    const selection = selectReviewOverview(model, [], new Set(), "从高到低");

    expect(selection.students.map((item) => item.id)).toEqual(["student-high", "student-low"]);
    expect(reviewQueryNeedsIntentFallback(selection)).toBe(false);
  });

  it("uses a structured LLM intent when colloquial text misses local presets", () => {
    const local = selectReviewOverview(model, [], new Set(), "成绩排个名");
    expect(reviewQueryNeedsIntentFallback(local)).toBe(true);

    const structuredIntent = intent({
      sort: "score_desc",
      explanation: "按得分率从高到低排序",
    });
    const selection = selectReviewOverviewFromIntent(model, [], new Set(), structuredIntent);

    expect(selection.students.map((item) => item.id)).toEqual(["student-high", "student-low"]);
  });

  it("matches Q1 without also matching Q11 or Q1.1", () => {
    const mixedStudent = student("student-mixed", "Mixed", 80, [correction("Q1"), correction("Q11"), correction("Q1.1")]);
    const mixedModel = {
      ...model,
      students: [mixedStudent],
      questions: [questionSummary("Q1"), questionSummary("Q11"), questionSummary("Q1.1")],
    } as ResultsModel;

    const selection = selectReviewOverview(mixedModel, [], new Set(), "Q1");
    const nestedSelection = selectReviewOverview(mixedModel, [], new Set(), "Q1.1");
    const chineseSelection = selectReviewOverview(mixedModel, [], new Set(), "第1题");

    expect(Array.from(selection.matchedCellKeys)).toEqual([reviewCellKey("student-mixed", "Q1")]);
    expect(selection.questions.map((item) => item.id)).toEqual(["Q1"]);
    expect(Array.from(nestedSelection.matchedCellKeys)).toEqual([reviewCellKey("student-mixed", "Q1.1")]);
    expect(Array.from(chineseSelection.matchedCellKeys)).toEqual([reviewCellKey("student-mixed", "Q1")]);
  });

  it("keeps confirmed reviews, pending reviews, and annotations distinct", () => {
    const statusStudent = student("student-status", "Status", 80, [
      correction("Q1", { requires_human_review: true, provisional_score: 8, teacher_score: 8 }),
      correction("Q2", { requires_human_review: true }),
    ]);
    const statusModel = {
      ...model,
      students: [statusStudent],
      questions: [questionSummary("Q1"), questionSummary("Q2")],
    } as ResultsModel;
    const reviewItems = collectResultReviewItems(statusModel, statusModel.students);
    const annotations = new Set([reviewCellKey("student-status", "Q2")]);

    expect(selectReviewOverviewFromIntent(statusModel, reviewItems, annotations, intent({ review_status: "confirmed" })).questions.map((item) => item.id)).toEqual(["Q1"]);
    expect(selectReviewOverviewFromIntent(statusModel, reviewItems, annotations, intent({ review_status: "pending" })).questions.map((item) => item.id)).toEqual(["Q2"]);
    expect(selectReviewOverviewFromIntent(statusModel, reviewItems, annotations, intent({ annotated: true })).questions.map((item) => item.id)).toEqual(["Q2"]);
  });

  it("sorts by every review signal shown in the queue", () => {
    const twoLowConfidence = student("student-two", "Two", 80, [
      correction("Q1", { confidence: 0.4 }),
      correction("Q2", { confidence: 0.4 }),
    ]);
    const oneBackendFlag = student("student-one", "One", 80, [correction("Q1", { requires_human_review: true })]);
    const noSignals = student("student-none", "None", 80);
    const sortModel = {
      ...model,
      students: [oneBackendFlag, noSignals, twoLowConfidence],
      questions: [questionSummary("Q1"), questionSummary("Q2")],
    } as ResultsModel;
    const reviewItems = collectResultReviewItems(sortModel, sortModel.students);

    const selection = selectReviewOverview(sortModel, reviewItems, new Set(), "复核信号最多优先");

    expect(selection.students.map((item) => item.id)).toEqual(["student-two", "student-one", "student-none"]);
  });
});

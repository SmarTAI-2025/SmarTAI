import { describe, expect, it } from "vitest";
import type { Correction, FilterIntentResult } from "@/types";
import type { ResultsModel, StudentSummary } from "@/components/tasks/resultsModel";
import {
  reviewQueryNeedsIntentFallback,
  selectReviewOverview,
  selectReviewOverviewFromIntent,
} from "./reviewOverview";

function correction(qId: string): Correction {
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
  };
}

function student(id: string, name: string, percent: number): StudentSummary {
  const item = correction("Q1");
  return {
    id,
    name,
    corrections: [item],
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

    const intent: FilterIntentResult = {
      recognized: true,
      min_score_percent: null,
      max_score_percent: null,
      pass_status: null,
      low_confidence: false,
      review_status: null,
      disagreement: false,
      annotated: false,
      sort: "score_desc",
      question_tokens: [],
      text_terms: [],
      explanation: "按得分率从高到低排序",
    };
    const selection = selectReviewOverviewFromIntent(model, [], new Set(), intent);

    expect(selection.students.map((item) => item.id)).toEqual(["student-high", "student-low"]);
  });
});

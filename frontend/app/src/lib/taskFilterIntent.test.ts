import { describe, expect, it } from "vitest";
import type { FilterIntentResult, PreparationIssue, ProblemInfo, StudentSubmission } from "@/types";
import type { QuestionSummary, StudentSummary } from "@/components/tasks/resultsModel";
import { scopeResultQuestions } from "@/components/tasks/ResultQuestionQuery";
import { buildSubmissionQuestions } from "@/lib/submissionReview";
import { compareValues } from "./sortValues";
import { EMPTY_FILTER_INTENT, parseLocalTaskFilter, supportsFilterIntent } from "./taskFilterIntent";
import { selectPreparationQuestions, selectStudentAnswerQuestions, selectSubmissionQuestions } from "./taskPreparationFilter";

const intent = (patch: Partial<FilterIntentResult>): FilterIntentResult => ({ ...EMPTY_FILTER_INTENT, ...patch });
const problem = (number: string, maxScore: number | undefined, codes: PreparationIssue["code"][] = [], type = "计算题"): ProblemInfo => ({
  q_id: number, number, type, stem: "Compute a derivative", criterion: "Check the derivative", max_score: maxScore,
  preparation_issues: codes.map((code, index) => ({ issue_id: `${number}-${index}`, code, severity: "warning", field: "stem", message: code, status: "open" })),
} as ProblemInfo);
const problems = [problem("Q1", 10, ["low_confidence"]), problem("Q2", 5, ["low_confidence", "source_conflict"]), problem("Q10", undefined, ["source_conflict"], "proof")];
const student = (id: string, reviewed: boolean[]): StudentSubmission => ({ stu_id: id, stu_name: id,
  stu_ans: reviewed.map((done, index) => ({ q_id: `Q${index+1}`, number: String(index+1), type: "calculation", content: "answer", flag: [], review_status: done ? "confirmed" : "pending" })),
});

describe("shared Ask controls and local execution", () => {
  it.each(["按满分升序", "sort by maximum score ascending"])("interprets %s as ordering, not text matching", (query) => {
    const parsed = parseLocalTaskFilter(query, "question_preparation");
    expect(parsed?.sort).toBe("max_score_asc");
    expect(selectPreparationQuestions(problems, parsed).map((item) => item.q_id)).toEqual(["Q2", "Q1", "Q10"]);
  });
  it("keeps unknown maximum scores last when reversing order", () => {
    expect(selectPreparationQuestions(problems, intent({ sort: "max_score_desc" })).map((item) => item.q_id)).toEqual(["Q1", "Q2", "Q10"]);
    expect(compareValues(null, 0, "desc")).toBeGreaterThan(0);
    expect(compareValues(Number.NaN, 0, "asc")).toBeGreaterThan(0);
  });
  it("intersects independent low-confidence and source-conflict conditions", () => {
    expect(selectPreparationQuestions(problems, intent({ low_confidence: true, preparation_status: "source_conflict" })).map((item) => item.q_id)).toEqual(["Q2"]);
  });
  it("does not drop material or score conditions from a compound request", () => {
    const data = [problems[0], { ...problems[1], reference_answer: "given" }];
    expect(selectPreparationQuestions(data, intent({ min_max_score: 8, material_field: "answer", material_status: "missing" })).map((item) => item.q_id)).toEqual(["Q1"]);
  });
  it("does not classify non-programming questions as missing test cases", () => {
    expect(selectPreparationQuestions(problems, intent({ material_field: "tests", material_status: "missing" }))).toEqual([]);
  });
  it("uses exact question numbers and normalized bilingual types", () => {
    expect(selectPreparationQuestions(problems, intent({ question_tokens: ["第1题"] })).map((item) => item.q_id)).toEqual(["Q1"]);
    expect(selectPreparationQuestions(problems, intent({ question_types: ["calculation"] })).map((item) => item.q_id)).toEqual(["Q1", "Q2"]);
  });
  it("does not claim to understand an unsupported compound instruction", () => {
    expect(parseLocalTaskFilter("按满分升序且只要上周迟到的学生", "question_preparation")).toBeNull();
    expect(supportsFilterIntent(intent({ sort: "max_score_asc", min_score_percent: 0 }), "question_preparation")).toBe(false);
    expect(supportsFilterIntent(intent({ sort: "name_asc" }), "student_answer_review")).toBe(false);
  });
  it("requires all selected answers, not only one, to be reviewed", () => {
    const students = [student("S1", [true, false]), student("S2", [true, true]), student("S3", [])];
    const questions = buildSubmissionQuestions([problem("Q1", 5), problem("Q2", 10)], students);
    expect(selectSubmissionQuestions(students, questions, intent({ submission_status: "reviewed" }), "all").students.map((item) => item.stu_id)).toEqual(["S2"]);
    expect(selectSubmissionQuestions(students, [], intent({ submission_status: "reviewed" }), "all").students).toEqual([]);
  });
  it("filters only the current student's answers and reverses question navigation", () => {
    const a = student("S1", [true, false]);
    const b = student("S2", [true]);
    const questions = buildSubmissionQuestions([problem("Q1", 5), problem("Q2", 10)], [a,b]);
    expect(selectStudentAnswerQuestions(questions, a, intent({ submission_status: "missing" }))).toEqual([]);
    expect(selectStudentAnswerQuestions(questions, b, intent({ submission_status: "missing" })).map((q) => q.id)).toEqual(["Q2"]);
    expect(selectStudentAnswerQuestions(questions, a, intent({ sort: "question_desc" })).map((q) => q.id)).toEqual(["Q2", "Q1"]);
    expect(a.stu_id).toBe("S1");
  });
  it("uses individual scores rather than class averages in student/review details", () => {
    const question = { id: "Q1", label: "Q1", count: 2, avgScore: 5, avgPercent: 50, maxScore: 10, minScore: 1, maxObservedScore: 9, lowConfidenceCount: 1, reviewCount: 1,
      entries: [{ student: { id: "S1" } as StudentSummary, correction: { q_id: "Q1", score: 9, max_score: 10, confidence: 0.9 } },
        { student: { id: "S2" } as StudentSummary, correction: { q_id: "Q1", score: 1, max_score: 10, confidence: 0.2, requires_human_review: true } }],
    } as QuestionSummary;
    expect(scopeResultQuestions([question], "S1")[0]).toMatchObject({ avgScore: 9, avgPercent: 90, count: 1, lowConfidenceCount: 0 });
    expect(scopeResultQuestions([question], "S2")[0]).toMatchObject({ avgScore: 1, avgPercent: 10, count: 1, lowConfidenceCount: 1 });
    expect(question.entries).toHaveLength(2);
  });
});

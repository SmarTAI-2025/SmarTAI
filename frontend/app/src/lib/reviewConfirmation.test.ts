import { describe, expect, it } from "vitest";
import { answerReviewBlocked, answerReviewNextBlocked, questionReviewBlocked, questionReviewNextBlocked, questionReviewIssues, questionReviewBlockers, questionReviewConfirmed, questionIssueNeedsReview, recognitionIssueLabel, submissionReviewBlockers, submissionConfirmationBlockers, studentIdentityReviewBlocked } from "./reviewConfirmation";
import type { Task, ProblemInfo, StudentAnswerInfo, PreparationIssue, StudentSubmission } from "@/types";

const question = (overrides: Partial<ProblemInfo> = {}): ProblemInfo => ({ q_id: "q1", number: "1", type: "short", stem: "Question text", criterion: "Scoring rubric", reference_answer: "Reference answer", max_score: 10, ...overrides });
const issue = (overrides: Partial<PreparationIssue> = {}): PreparationIssue => ({ issue_id: "i1", q_id: "q1", field: "stem", code: "recognition_needs_review", severity: "warning", status: "open", ...overrides });

describe("review hard failures", () => {
  it("keeps a new shared rubric explanation visible after material confirmation without reopening its gate", () => {
    const warning = issue({
      code: "math_rubric_advisory", field: "rubric",
      details: { message: "本轮 AI 评分解释：原有明确规则优先；后续错误不重复扣分。" },
    });
    const problem = question({
      review_status: "confirmed", max_score_review_status: "confirmed",
      preparation_issues: [warning],
    });
    expect(questionReviewIssues(problem)).toEqual([warning]);
    expect(recognitionIssueLabel(warning, "zh-CN")).toBe(warning.details?.message);
    expect(questionReviewConfirmed(problem)).toBe(true);
    expect(questionReviewBlocked(problem)).toBe(false);
    expect(questionReviewNextBlocked(problem)).toBe(false);
    expect(questionReviewBlockers([problem], "T1", "zh-CN")).toEqual([]);
  });

  it("allows a genuine blank response but blocks failed recognition", () => {
    const blank = {q_id:"q1", number:"1", type:"short", content:"", flag:[]} as StudentAnswerInfo;
    expect(answerReviewBlocked(blank)).toBe(false);
    expect(answerReviewBlocked({...blank, flag:["recognition_failed"]})).toBe(true);
  });
  it("allows uncertain content and blocks an empty question", () => {
    expect(questionReviewBlocked({stem:"content", criterion:"Scoring rubric", preparation_issues:[{status:"open", severity:"warning"}]} as ProblemInfo)).toBe(false);
    expect(questionReviewBlocked({stem:""} as ProblemInfo)).toBe(true);
  });
  it("routes a source with no student record to recovery and a failed answer to its exact question", () => {
    const task = {task_id:"T1", submission_sources:[{source_id:"source-1", file_name:"sample.pdf", status:"failed"}], student_data:{internal:{stu_id:"S1", stu_name:"Sample", stu_ans:[{q_id:"q1", content:"", flag:["recognition_failed"]}]}}} as unknown as Task;
    const issues=submissionReviewBlockers(task,"en-US");
    expect(issues).toHaveLength(2);
    expect(issues[0].href).toContain("/submissions/upload");
    expect(issues[1].href).toBe("/tasks/T1/students/S1?question=q1");
  });

  it("keeps source-wide uncertainty and ordinary confirmation out of the question risk queue", () => {
    const warnings = [issue(), issue({ code: "recognition_partial", details: { coverage: { unverified_targets: ["1", "2"] } } }),
      issue({ details: { confidence_reasons: ["quality_uncalibrated"] } })];
    const problems = warnings.map((warning, index) => question({ q_id: `q${index}`, review_status: "needs_review", preparation_issues: [warning] }));
    expect(warnings.every(warning => !questionIssueNeedsReview(warning))).toBe(true);
    expect(problems.flatMap(questionReviewIssues)).toEqual([]);
    expect(questionReviewBlockers(problems, "T1", "zh-CN")).toEqual([]);
    expect(problems[0].preparation_issues).toEqual([warnings[0]]);
  });

  it.each([0, 0.2499, 0.25, 0.5999, 0.60, 1, undefined, NaN, -1])("puts all numeric confidence below .6 into suggestions without blocking (%s)", (confidence) => {
    const warning = issue({ code: "low_confidence", details: { confidence } });
    const problem = question({ preparation_issues: [warning] });
    const suggested = typeof confidence === "number" && confidence >= 0 && confidence < 0.6;
    expect(questionReviewBlocked(problem)).toBe(false);
    expect(questionReviewNextBlocked(problem)).toBe(false);
    expect(questionReviewIssues(problem)).toHaveLength(suggested ? 1 : 0);
    expect(questionReviewBlockers([problem], "T1", "zh-CN")).toHaveLength(0);
    expect(questionReviewNextBlocked({ ...problem, review_status: "confirmed" })).toBe(false);
    if (suggested) expect(questionReviewIssues(problem)[0]).toMatchObject({ severity: "warning" });
  });

  it("uses actual material confidence and keeps the confirm action possible", () => {
    const imported = question({ preparation_issues: [issue({ code: "low_confidence", field: "rubric" })], material_provenance: { criterion: { confidence: 0.2 } as NonNullable<ProblemInfo["material_provenance"]>["criterion"] } });
    expect(questionReviewBlocked(imported)).toBe(false);
    expect(questionReviewNextBlocked(imported)).toBe(false);
    expect(questionReviewIssues(imported)[0].details?.confidence).toBe(0.2);
  });

  it("does not convert categorical file-level low into a numeric mandatory gate", () => {
    const warning = issue({ details: { confidence_reasons: ["quality_uncalibrated", "low_confidence_content"] } });
    const problem = question({ preparation_issues: [warning] });
    expect(questionIssueNeedsReview(warning)).toBe(false);
    expect(questionReviewIssues(problem)).toEqual([]);
    expect(questionReviewNextBlocked(problem)).toBe(false);
  });

  it("does not revive a resolved confidence suggestion as a blocker", () => {
    const warning = issue({ code: "low_confidence", status: "resolved", details: { confidence: 0.2 } });
    const edited = question({ review_status: "edited", preparation_issues: [warning] });
    expect(questionReviewBlocked(edited)).toBe(false);
    expect(questionReviewNextBlocked(edited)).toBe(false);
    expect(questionReviewIssues(edited)).toEqual([]);
    expect(questionReviewNextBlocked({ ...edited, review_status: "confirmed" })).toBe(false);
    expect(questionReviewIssues({ ...edited, review_status: "confirmed" })).toEqual([]);
  });

  it.each(["failed_pages", "unprocessed_pages", "missing_targets"])("requires repair of concrete question coverage failure: %s", (key) => {
    const warning = issue({ code: "recognition_partial", details: { coverage: { [key]: [2] } } });
    const problem = question({ review_status: "confirmed", preparation_issues: [warning] });
    expect(questionIssueNeedsReview(warning)).toBe(true);
    expect(questionReviewBlocked(problem)).toBe(true);
    expect(questionReviewNextBlocked(problem)).toBe(true);
    expect(questionReviewIssues(problem)[0]).toMatchObject({ severity: "blocking" });
    expect(warning.severity).toBe("warning");
  });

  it("surfaces empty question text and conflicting subpart points even without server issues", () => {
    const empty = question({ stem: " " });
    const conflict = question({ rubric_point_summary: { contract_version: 1, has_explicit_subpart_points: true, items: [], major_max_score: "10", total_points: "9", is_valid: false, issue_code: "rubric_subpart_points_mismatch" } });
    expect(questionReviewIssues(empty)[0]).toMatchObject({ severity: "blocking", field: "stem" });
    expect(questionReviewIssues(conflict)[0]).toMatchObject({ severity: "blocking", field: "rubric" });
    expect(questionReviewBlocked(conflict)).toBe(true);
    expect(questionReviewBlockers([empty, conflict], "T1", "en-US")).toHaveLength(2);
  });

  it("requires a usable rubric but only suggests completing a reference answer", () => {
    const missingRubric = question({ criterion: "", reference_answer: "" });
    expect(questionReviewIssues(missingRubric).map(issue => [issue.field, issue.severity])).toEqual([["rubric", "blocking"], ["answer", "warning"]]);
    expect(questionReviewNextBlocked(missingRubric)).toBe(true);
    const missingAnswer = question({ reference_answer: "", preparation_issues: [issue({ code: "manual_completion_required", severity: "blocking", details: { required_fields: ["answer"] } })] });
    expect(questionReviewNextBlocked(missingAnswer)).toBe(false);
    expect(questionReviewIssues(missingAnswer).every(issue => issue.severity === "warning")).toBe(true);
    expect(questionReviewIssues({ ...missingAnswer, review_status: "confirmed" })).toEqual([]);
  });

  it("lets a repaired question be confirmed before allowing the next stage", () => {
    const repaired = question({ review_status: "edited", preparation_issues: [issue({ code: "required_review_confirmation", severity: "blocking", status: "resolved" })] });
    expect(questionReviewBlocked(repaired)).toBe(false);
    expect(questionReviewNextBlocked(repaired)).toBe(true);
    expect(questionReviewIssues(repaired)[0]).toMatchObject({ severity: "blocking", details: { review_display_reason: "confirmation_required" } });
    expect(questionReviewNextBlocked({ ...repaired, review_status: "confirmed" })).toBe(false);
  });

  it.each(["manual_completion_required", "source_conflict", "ambiguous_question_match"] as const)("allows explicit confirmation of usable content with a %s marker", code => {
    const marker = issue({ code, severity: "warning", details: { required_fields: ["criterion"] } });
    const ready = question({ preparation_issues: [marker] });
    expect(questionReviewBlocked(ready)).toBe(false);
    expect(questionReviewNextBlocked(ready)).toBe(true);
    expect(questionReviewBlocked({ ...ready, criterion: "" })).toBe(true);
    expect(questionReviewNextBlocked({ ...ready, review_status: "confirmed", preparation_issues: [{ ...marker, status: "acknowledged" }] })).toBe(false);
  });

  it("requires confirmation of a repaired answer without rejecting that confirmation", () => {
    const repaired = { q_id: "q1", number: "1", type: "short", content: "corrected", flag: ["recognition_repaired_needs_confirmation"] };
    expect(answerReviewBlocked(repaired)).toBe(false);
    expect(answerReviewNextBlocked(repaired)).toBe(true);
    const task = { task_id: "T1", student_data: { s1: { stu_id: "s1", stu_name: "Sample", stu_ans: [repaired] } } } as unknown as Task;
    expect(submissionReviewBlockers(task, "en-US")).toHaveLength(1);
    expect(submissionConfirmationBlockers(task, "en-US")).toHaveLength(0);
    expect(answerReviewNextBlocked({ ...repaired, review_status: "confirmed" })).toBe(false);
  });

  it("keeps the legacy very-low-confidence marker optional", () => {
    const answer = { q_id: "q1", number: "1", type: "short", content: "Visible work", flag: ["recognition_very_low_confidence"] };
    expect(answerReviewBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked({ ...answer, review_status: "confirmed" })).toBe(false);
  });

  it.each(["very_low_confidence", "recognition_low_confidence", "low_confidence"])("does not turn uncalibrated %s into a mandatory review", (flag) => {
    const answer = { q_id: "q1", number: "1", type: "short", content: "original text", flag: [flag] };
    const task = { task_id: "T1", student_data: { s1: { stu_id: "s1", stu_name: "Sample", stu_ans: [answer] } } } as unknown as Task;
    expect(answerReviewBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked(answer)).toBe(false);
    expect(submissionConfirmationBlockers(task, "en-US")).toEqual([]);
    expect(submissionReviewBlockers(task, "en-US")).toEqual([]);
  });

  it("blocks source-backed identity problems and placeholders, but not legacy optional confirmation", () => {
    const student: StudentSubmission = { stu_id: "s1", stu_name: "Sample", stu_ans: [], identity_status: "needs_review" };
    expect(studentIdentityReviewBlocked(student)).toBe(false);
    expect(studentIdentityReviewBlocked({ ...student, source_id: "source-1" })).toBe(true);
    expect(studentIdentityReviewBlocked({ ...student, source_id: "source-1", identity_status: "matched" })).toBe(false);
    expect(studentIdentityReviewBlocked({ ...student, stu_name: "[Unknown Student]", identity_status: "matched" })).toBe(true);
    expect(studentIdentityReviewBlocked({ ...student, stu_id: "[Unknown Student]", identity_status: "matched" })).toBe(true);
    for (const value of ["Unknown Student", "unknown student", "[unknown student]", "未知学生"]) {
      expect(studentIdentityReviewBlocked({ ...student, stu_id: value, stu_name: value, identity_status: "matched" })).toBe(false);
    }
    const task = { task_id: "T1", student_data: { s1: { ...student, stu_name: "[Unknown Student]" } } } as unknown as Task;
    expect(submissionConfirmationBlockers(task, "en-US")).toHaveLength(1);
  });

  it("keeps an empty manual review in the matrix so student rows can be created", () => {
    const task = { task_id: "T1", submission_sources: [{ source_id: "source-1", file_name: "sample.pdf", status: "identity_needs_review", internal_status: "identity_conflict" }], student_data: {} } as unknown as Task;
    expect(submissionReviewBlockers(task, "zh-CN")[0].href).toBe("/tasks/T1/submissions#student-count");
    expect(submissionConfirmationBlockers(task, "zh-CN")).toHaveLength(1);
  });
});

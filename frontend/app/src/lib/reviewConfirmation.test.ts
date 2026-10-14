import { describe, expect, it } from "vitest";
import { answerReviewBlocked, answerReviewNextBlocked, questionReviewBlocked, questionReviewNextBlocked, questionReviewIssues, questionReviewBlockers, questionIssueNeedsReview, submissionReviewBlockers, submissionConfirmationBlockers, studentIdentityReviewBlocked } from "./reviewConfirmation";
import type { Task, ProblemInfo, StudentAnswerInfo, PreparationIssue, StudentSubmission } from "@/types";

const question = (overrides: Partial<ProblemInfo> = {}): ProblemInfo => ({ q_id: "q1", number: "1", type: "short", stem: "Question text", criterion: "", max_score: 10, ...overrides });
const issue = (overrides: Partial<PreparationIssue> = {}): PreparationIssue => ({ issue_id: "i1", q_id: "q1", field: "stem", code: "recognition_needs_review", severity: "warning", status: "open", ...overrides });

describe("review hard failures", () => {
  it("allows a genuine blank response but blocks failed recognition", () => {
    const blank = {q_id:"q1", number:"1", type:"short", content:"", flag:[]} as StudentAnswerInfo;
    expect(answerReviewBlocked(blank)).toBe(false);
    expect(answerReviewBlocked({...blank, flag:["recognition_failed"]})).toBe(true);
  });
  it("allows uncertain content and blocks an empty question", () => {
    expect(questionReviewBlocked({stem:"content", preparation_issues:[{status:"open", severity:"warning"}]} as ProblemInfo)).toBe(false);
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
      issue({ code: "default_max_score_requires_review" }), issue({ code: "low_confidence", details: { confidence: 0.6 } })];
    const problems = warnings.map((warning, index) => question({ q_id: `q${index}`, review_status: "needs_review", preparation_issues: [warning] }));
    expect(warnings.every(warning => !questionIssueNeedsReview(warning))).toBe(true);
    expect(problems.flatMap(questionReviewIssues)).toEqual([]);
    expect(questionReviewBlockers(problems, "T1", "zh-CN")).toEqual([]);
    expect(problems[0].preparation_issues).toEqual([warnings[0]]);
  });

  it("shows specific very-low-confidence evidence without blocking the next stage", () => {
    const warning = issue({ code: "low_confidence", details: { confidence: 0.2 } });
    const problem = question({ preparation_issues: [warning] });
    expect(questionReviewIssues(problem)).toEqual([warning]);
    expect(questionReviewNextBlocked(problem)).toBe(false);
    expect(questionIssueNeedsReview({ ...warning, details: { confidence: Number.NaN } })).toBe(false);
    const imported = question({ preparation_issues: [{ ...warning, field: "rubric", details: undefined }], material_provenance: { criterion: { confidence: 0.2 } as NonNullable<ProblemInfo["material_provenance"]>["criterion"] } });
    expect(questionReviewIssues(imported)).toHaveLength(1);
    expect(questionReviewNextBlocked(imported)).toBe(false);
  });

  it("surfaces empty question text and conflicting subpart points even without server issues", () => {
    const empty = question({ stem: " " });
    const conflict = question({ rubric_point_summary: { contract_version: 1, has_explicit_subpart_points: true, items: [], major_max_score: "10", total_points: "9", is_valid: false, issue_code: "rubric_subpart_points_mismatch" } });
    expect(questionReviewIssues(empty)[0]).toMatchObject({ severity: "blocking", field: "stem" });
    expect(questionReviewIssues(conflict)[0]).toMatchObject({ severity: "blocking", field: "rubric" });
    expect(questionReviewBlocked(conflict)).toBe(true);
    expect(questionReviewBlockers([empty, conflict], "T1", "en-US")).toHaveLength(2);
  });

  it("lets a repaired question be confirmed before allowing the next stage", () => {
    const repaired = question({ review_status: "edited", preparation_issues: [issue({ severity: "blocking", status: "resolved" })] });
    expect(questionReviewBlocked(repaired)).toBe(false);
    expect(questionReviewNextBlocked(repaired)).toBe(true);
    expect(questionReviewIssues(repaired)[0]).toMatchObject({ severity: "blocking", details: { review_display_reason: "confirmation_required" } });
    expect(questionReviewNextBlocked({ ...repaired, review_status: "confirmed" })).toBe(false);
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

  it("routes an unresolved source without a student record back to source recovery", () => {
    const task = { task_id: "T1", submission_sources: [{ source_id: "source-1", file_name: "sample.pdf", status: "identity_needs_review", internal_status: "identity_conflict" }], student_data: {} } as unknown as Task;
    expect(submissionReviewBlockers(task, "zh-CN")[0].href).toBe("/tasks/T1/submissions/upload#source-source-1");
    expect(submissionConfirmationBlockers(task, "zh-CN")).toHaveLength(1);
  });
});

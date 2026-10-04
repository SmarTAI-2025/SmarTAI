import { describe, expect, it } from "vitest";
import { answerReviewBlocked, questionReviewBlocked, submissionReviewBlockers } from "./reviewConfirmation";
import type { Task, ProblemInfo, StudentAnswerInfo } from "@/types";
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
    const task = {task_id:"T1", submission_sources:[{source_id:"source-1", file_name:"sample.pdf", status:"failed"}], student_data:{internal:{stu_id:"S1", stu_ans:[{q_id:"q1", content:"", flag:["recognition_failed"]}]}}} as unknown as Task;
    const issues=submissionReviewBlockers(task,"en-US");
    expect(issues).toHaveLength(2);
    expect(issues[0].href).toContain("/submissions/upload");
    expect(issues[1].href).toBe("/tasks/T1/students/S1?question=q1");
  });
});

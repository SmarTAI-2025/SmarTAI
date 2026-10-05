import { describe, expect, it } from "vitest";
import { formatSubmissionFlag, getAnswerState, getSubmissionReviewStats, selectSubmissionReview } from "./submissionReview";

describe("OCR review labels", () => {
  it("counts every unconfirmed answer, including optional checks and genuine blanks", () => {
    const questions = ["q1", "q2", "q3", "q4"].map(id => ({ id, label: id, type: "", stem: "" }));
    const students = [{ stu_id: "s1", stu_name: "Sample", identity_status: "matched" as const, stu_ans: [
      { q_id: "q1", number: "q1", type: "short", content: "normal", flag: ["external_annotation_present"] },
      { q_id: "q2", number: "q2", type: "short", content: "", flag: [] },
      { q_id: "q3", number: "q3", type: "short", content: "uncertain", flag: ["recognition_needs_review"] },
    ] }];
    expect(getSubmissionReviewStats(students, questions).reviewCells).toBe(3);
    expect(getAnswerState(students[0].stu_ans[0])).toBe("recognized");
    expect(selectSubmissionReview(students, questions, "", "review", "student_id").students).toHaveLength(1);
    const confirmed = [{ ...students[0], stu_ans: students[0].stu_ans.map(a => ({ ...a, review_status: "confirmed" as const })) }];
    expect(getSubmissionReviewStats(confirmed, questions).reviewCells).toBe(0);
    expect(selectSubmissionReview(confirmed, questions, "", "review", "student_id").students).toHaveLength(0);
    expect(selectSubmissionReview([{ ...confirmed[0], identity_status: "needs_review" }], questions, "", "review", "student_id").students).toHaveLength(1);
    expect(getAnswerState({ q_id: "q4", number: "q4", type: "short", content: "", flag: ["recognition_failed"], review_status: "confirmed" })).toBe("flagged");
  });
  it("distinguishes required review from a claim of low accuracy", () => {
    expect(formatSubmissionFlag("recognition_needs_review", "zh-CN")).toBe("识别结果需对照原件核对");
    expect(formatSubmissionFlag("external_annotation_present", "en-US")).toContain("not student answers");
    expect(formatSubmissionFlag("authorship_uncertain", "zh-CN")).toContain("归属不明");
    expect(formatSubmissionFlag("unfamiliar observation", "zh-CN")).toBe("unfamiliar observation");
  });

  it("retains source flags after review without keeping the cell pending", () => {
    const answer = { q_id: "q1", number: "1", type: "proof", content: "0*x=0", flag: ["recognition_needs_review"] };
    expect(getAnswerState(answer)).toBe("flagged");
    expect(getAnswerState({ ...answer, review_status: "confirmed" })).toBe("reviewed");
    expect(answer.flag).toEqual(["recognition_needs_review"]);
  });
});

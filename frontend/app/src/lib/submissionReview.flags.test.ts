import { describe, expect, it } from "vitest";
import { answerNeedsConfirmation, answerReviewAttention, answerReviewNotes, formatSubmissionFlag, getAnswerState, getSubmissionReviewStats, selectSubmissionReview, studentNeedsAttention } from "./submissionReview";

describe("OCR review labels", () => {
  it("counts every unconfirmed answer, including optional checks and genuine blanks", () => {
    const questions = ["q1", "q2", "q3", "q4"].map(id => ({ id, label: id, type: "", stem: "" }));
    const students = [{ stu_id: "s1", stu_name: "Sample", identity_status: "matched" as const, stu_ans: [
      { q_id: "q1", number: "q1", type: "short", content: "normal", flag: ["external_annotation_present"] },
      { q_id: "q2", number: "q2", type: "short", content: "", flag: [] },
      { q_id: "q3", number: "q3", type: "short", content: "uncertain", flag: ["recognition_needs_review"] },
    ] }];
    expect(getSubmissionReviewStats(students, questions).reviewCells).toBe(3);
    expect(getAnswerState(students[0].stu_ans[0])).toBe("flagged");
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
    expect(getAnswerState(answer)).toBe("recognized");
    expect(getAnswerState({ ...answer, review_status: "confirmed" })).toBe("reviewed");
    expect(answer.flag).toEqual(["recognition_needs_review"]);
  });

  it("does not turn document-wide warnings or ordinary pending answers into risks", () => {
    const answers = Array.from({ length: 12 }, (_, index) => ({ q_id: `q${index}`, number: `${index}`, type: "proof", content: "Readable answer", flag: ["recognition_needs_review"] }));
    expect(answers.every(answer => answerReviewAttention(answer) === "none")).toBe(true);
    expect(answers.every(answer => getAnswerState(answer) === "recognized" && answerNeedsConfirmation(answer))).toBe(true);
    expect(studentNeedsAttention({ stu_id: "s1", stu_name: "Sample", stu_ans: answers }, answers.map(answer => ({ id: answer.q_id, label: answer.number, type: answer.type, stem: "" })))).toBe(false);
  });

  it.each(["very_low_confidence", "recognition_low_confidence", "low_confidence"])("retires uncalibrated %s from all review advice", (flag) => {
    const answer = { q_id: "q1", number: "1", type: "proof", content: "Readable answer", flag: [flag] };
    expect(answerReviewAttention(answer)).toBe("none");
    expect(answerReviewNotes(answer, "zh-CN")).toEqual([]);
    expect(answerReviewAttention({ ...answer, flag: ["recognition_failed"] })).toBe("blocking");
    expect(answerReviewAttention({ ...answer, flag: ["recognition_repaired_needs_confirmation"] })).toBe("blocking");
    expect(answerReviewAttention({ ...answer, review_status: "confirmed" })).toBe("none");
  });

  it.each([0, .25, .5999, .6, .95])("uses real confidence instead of stale flags (%s)", recognition_confidence => {
    const answer = { q_id: "q1", number: "1", type: "proof", content: "Partial text retained", recognition_confidence, flag: ["low_confidence"] };
    expect(answerReviewAttention(answer)).toBe(recognition_confidence < .6 ? "advisory" : "none");
    expect(answerReviewAttention({ ...answer, flag: ["authorship_uncertain"] })).toBe("advisory");
    const incomplete = { ...answer, flag: ["recognition_manual_pending", "recognition_unprocessed_pages:2"] };
    expect(answerReviewAttention(incomplete)).toBe("blocking");
    expect(answerReviewNotes(incomplete, "en-US").map(note => note.severity).sort()).toEqual(recognition_confidence < .6 ? ["advisory", "blocking"] : ["blocking"]);
    expect(answerReviewNotes(incomplete, "en-US").some(note => note.label.includes("pages:2"))).toBe(false);
  });

  it("shows advice only on the low-confidence answer, across questions and students", () => {
    const scores = [[.96, .3], [.6, null]];
    const attention = scores.map(student => student.map((recognition_confidence, i) => {
      const answer = { q_id: `q${i}`, number: `${i}`, type: "proof", content: "Literal work",
        recognition_confidence, flag: ["recognition_low_confidence"] };
      const notes = answerReviewNotes(answer, "zh-CN");
      expect(notes).toHaveLength(recognition_confidence === .3 ? 1 : 0);
      return answerReviewAttention(answer);
    }));
    expect(attention).toEqual([["none", "advisory"], ["none", "none"]]);
  });

  it.each(["recognition_failed_pages:2", "recognition_unprocessed_pages:2"])("keeps server-projected page failure %s blocked after confirmation", (flag) => {
    const answer = { q_id: "q1", number: "1", type: "proof", content: "", flag: ["recognition_needs_review", flag, "recognition_failed"], review_status: "confirmed" as const };
    expect(answerReviewAttention(answer)).toBe("blocking");
    expect(getAnswerState(answer)).toBe("flagged");
  });
});

import { describe, expect, it } from "vitest";
import { formatSubmissionFlag, getAnswerState } from "./submissionReview";

describe("OCR review labels", () => {
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

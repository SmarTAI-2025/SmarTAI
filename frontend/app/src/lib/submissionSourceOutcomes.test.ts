import { describe, expect, it } from "vitest";
import type { SubmissionSourceOutcome } from "@/types";
import { getSubmissionSourceReasonCopy } from "./submissionSourceOutcomes";

function source(overrides: Partial<SubmissionSourceOutcome>): SubmissionSourceOutcome {
  return {
    source_id: "src-1",
    file_id: "file-1",
    file_name: "student.png",
    content_type: "image/png",
    size_bytes: 1024,
    status: "failed",
    internal_status: "parse_failed",
    reason_code: "submission_parse_failed",
    failure_phase: "recognition",
    retryable: true,
    matched_answer_count: 0,
    unknown_question_ids: [],
    job_id: "job-1",
    attempt: 1,
    created_at: 1,
    ...overrides,
  };
}

describe("submission source reason copy", () => {
  it("states that a vision-provider failure is an OCR capability problem", () => {
    const copy = getSubmissionSourceReasonCopy(source({
      reason_code: "vision_provider_required",
      failure_phase: "ocr",
    }), "zh-CN");

    expect(copy.title).toContain("模型不支持图片 OCR");
    expect(copy.description).toContain("并不是文件丢失或学生答案有误");
    expect(copy.nextStep).toContain("视觉模型");
  });

  it("states that no_matching_answer is not a wrong student answer", () => {
    const copy = getSubmissionSourceReasonCopy(source({
      internal_status: "no_matching_answer",
      reason_code: "no_matching_answer",
      failure_phase: "question_matching",
      unknown_question_ids: ["q99"],
    }), "zh-CN");

    expect(copy.description).toContain("不是“学生答错了”");
    expect(copy.description).toContain("传错作业");
    expect(copy.nextStep).toContain("q99");
  });

  it("distinguishes no detected answers from unmatched question IDs", () => {
    const copy = getSubmissionSourceReasonCopy(source({
      internal_status: "no_matching_answer",
      reason_code: "no_answer_content_detected",
      failure_phase: "answer_detection",
    }), "zh-CN");

    expect(copy.title).toContain("没有提取到任何作答内容");
    expect(copy.description).toContain("与“题号不匹配”不同");
    expect(copy.description).toContain("不表示学生答错");
  });

  it("explains that duplicate identities preserve both files", () => {
    const copy = getSubmissionSourceReasonCopy(source({
      status: "identity_needs_review",
      internal_status: "identity_conflict",
      reason_code: "duplicate_student_identity",
      failure_phase: "identity",
      student_candidate: "S003",
    }), "zh-CN");

    expect(copy.description).toContain("没有用后一份覆盖前一份");
    expect(copy.nextStep).toContain("修正身份");
  });
});

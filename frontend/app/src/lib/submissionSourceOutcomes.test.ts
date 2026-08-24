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
  it.each([
    [
      "submission_model_field_too_long",
      "structured_parse",
      "模型返回字段超过安全长度",
      "The model returned an oversized field",
    ],
    [
      "submission_outcome_persistence_failed",
      "outcome_persistence",
      "逐文件识别结果保存未完成",
      "Per-file outcomes were not fully saved",
    ],
    [
      "student_identity_conflict",
      "identity",
      "学生身份发生冲突",
      "The student identity conflicts",
    ],
    [
      "submission_source_content_type_mismatch",
      "source_read",
      "文件内容与扩展名或类型不一致",
      "The file contents do not match its name or declared type",
    ],
    [
      "pdf_extraction_busy",
      "source_read",
      "PDF 读取服务正忙",
      "PDF extraction is busy",
    ],
    [
      "submission_archive_member_too_large",
      "archive",
      "压缩包中的这份文件过大",
      "This archive member is too large",
    ],
    [
      "submission_archive_member_unreadable",
      "archive",
      "压缩包中的这份文件无法读取",
      "This archive member could not be read",
    ],
    [
      "submission_archive_member_unsafe_path",
      "archive",
      "压缩包成员路径不安全",
      "This archive member has an unsafe path",
    ],
    [
      "provider_endpoint_non_public_address",
      "ocr",
      "OCR：中转站域名解析到非公网地址",
      "OCR: The relay hostname resolved to a non-public address",
    ],
  ])("has exact bilingual copy for newly public reason %s", (
    reasonCode,
    failurePhase,
    expectedZhTitle,
    expectedEnTitle,
  ) => {
    const overrides: Partial<SubmissionSourceOutcome> = {
      reason_code: reasonCode,
      failure_phase: failurePhase,
    };
    if (reasonCode === "student_identity_conflict") {
      overrides.status = "identity_needs_review";
      overrides.internal_status = "identity_conflict";
      overrides.student_candidate = "S008";
    }

    const zhCopy = getSubmissionSourceReasonCopy(source(overrides), "zh-CN");
    const enCopy = getSubmissionSourceReasonCopy(source(overrides), "en-US");

    expect(zhCopy.title).toBe(expectedZhTitle);
    expect(enCopy.title).toBe(expectedEnTitle);
    expect(zhCopy.title).not.toBe("这份作答未完成识别");
    expect(enCopy.title).not.toBe("This submission was not recognized");
    expect(zhCopy.description).not.toBe("");
    expect(enCopy.description).not.toBe("");
    expect(zhCopy.nextStep).not.toBe("");
    expect(enCopy.nextStep).not.toBe("");
  });

  it("states that a vision-provider failure is an OCR capability problem", () => {
    const copy = getSubmissionSourceReasonCopy(source({
      reason_code: "vision_provider_required",
      failure_phase: "ocr",
    }), "zh-CN");

    expect(copy.title).toContain("模型不支持图片 OCR");
    expect(copy.description).toContain("并不是文件丢失或学生答案有误");
    expect(copy.nextStep).toContain("视觉模型");
  });

  it("identifies a provider timeout in the OCR phase as an OCR model failure", () => {
    const zhCopy = getSubmissionSourceReasonCopy(source({
      reason_code: "provider_timeout",
      failure_phase: "ocr",
    }), "zh-CN");
    const enCopy = getSubmissionSourceReasonCopy(source({
      reason_code: "provider_timeout",
      failure_phase: "ocr",
    }), "en-US");

    expect(zhCopy.title).toBe("OCR：模型响应超时");
    expect(zhCopy.description).toContain("OCR 模型调用");
    expect(enCopy.title).toBe("OCR: The model timed out");
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

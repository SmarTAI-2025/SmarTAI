import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { SubmissionSourceOutcome } from "@/types";
import { SubmissionSourceOutcomePanel } from "./SubmissionSourceOutcomePanel";

const sources: SubmissionSourceOutcome[] = [
  {
    source_id: "src-good",
    file_id: "file-good",
    file_name: "student-good.txt",
    content_type: "text/plain",
    size_bytes: 128,
    status: "parsed",
    internal_status: "parsed",
    retryable: false,
    matched_answer_count: 2,
    unknown_question_ids: [],
    job_id: "job-21",
    attempt: 1,
    created_at: 1,
  },
  {
    source_id: "src-ocr",
    file_id: "file-ocr",
    file_name: "student-scan.png",
    content_type: "image/png",
    size_bytes: 4096,
    status: "failed",
    internal_status: "parse_failed",
    reason_code: "provider_vision_not_supported",
    failure_phase: "ocr",
    retryable: false,
    matched_answer_count: 0,
    unknown_question_ids: [],
    job_id: "job-21",
    attempt: 1,
    trace_id: "job-21:1:src-ocr",
    created_at: 1,
  },
  {
    source_id: "src-identity",
    file_id: "file-identity",
    file_name: "student-unknown.pdf",
    content_type: "application/pdf",
    size_bytes: 2048,
    status: "identity_needs_review",
    internal_status: "identity_conflict",
    reason_code: "identity_needs_review",
    failure_phase: "identity",
    retryable: false,
    student_candidate: "S008",
    matched_answer_count: 1,
    unknown_question_ids: [],
    job_id: "job-21",
    attempt: 1,
    created_at: 1,
  },
];

describe("SubmissionSourceOutcomePanel", () => {
  it("keeps manually pending sources expanded and distinguishes a stop from a provider failure", () => {
    render(<MemoryRouter><SubmissionSourceOutcomePanel locale="zh-CN" taskId="task" sources={[{
      ...sources[1], status: "manual_review", reason_code: "operation_cancelled", recognition_reason_code: "operation_cancelled",
      resolution_status: "manual_review", failure_phase: "recognition",
    }]} /></MemoryRouter>);
    expect(screen.getByText("识别未完成，等待人工处理").closest("details")).toHaveAttribute("open");
    expect(screen.getByText(/1 份待人工处理/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "重新选择文件" })).not.toBeInTheDocument();
  });
  it.each(["zh-CN", "en-US"] as const)("shows retained, failed and unread pages with the actual provider cause in %s", (locale) => {
    render(<MemoryRouter><SubmissionSourceOutcomePanel locale={locale} sources={[{
      ...sources[1], content_type: "application/pdf", file_name: "student.pdf",
      reason_code: "provider_submit_uncertain", failure_phase: "recognition",
      page_diagnostics: { total_pages: 3, processed_pages: [1], failed_pages: [2], unprocessed_pages: [3],
        page_errors: [{ page: 2, error_code: "provider_upstream_unavailable", status_code: 503 }] },
    }]} /></MemoryRouter>);
    const zh = locale === "zh-CN";
    expect(screen.getByText(zh ? "读取成功页码：" : "Successfully read pages:").parentElement).toHaveTextContent(zh ? "读取成功页码：1" : "Successfully read pages: 1");
    expect(screen.getByText(zh ? "读取失败页码：" : "Failed pages:").parentElement).toHaveTextContent(zh ? "读取失败页码：2" : "Failed pages: 2");
    expect(screen.getByText(zh ? "尚未读取页码：" : "Unread pages:").parentElement).toHaveTextContent(zh ? "尚未读取页码：3" : "Unread pages: 3");
    expect(screen.getByText(zh ? "第 2 页：模型服务暂时不可用 (HTTP 503)" : "Page 2: The model service is temporarily unavailable (HTTP 503)")).toBeInTheDocument();
    expect(screen.getByText(zh ? "成功页结果已保留；文件与模型设置未变时，重试会复用可用结果，补读其余页面。" : "Successful page results are retained. If the file and model settings are unchanged, retry reuses eligible results and reads the remaining pages.")).toBeInTheDocument();
    expect(screen.getByText(zh ? /再次调用可能重复消耗额度/ : /Calling again may use quota twice/)).toBeInTheDocument();
    expect(screen.queryByText(/学生未作答|内容为空|student left.*blank/i)).not.toBeInTheDocument();
  });

  it("does not invent an HTTP status for older page diagnostics", () => {
    render(<MemoryRouter><SubmissionSourceOutcomePanel locale="zh-CN" sources={[{
      ...sources[1], reason_code: "question_source_incomplete", failure_phase: "recognition",
      page_diagnostics: { total_pages: 2, processed_pages: [1], failed_pages: [2], unprocessed_pages: [],
        page_errors: [{ page: 2, error_code: "provider_upstream_unavailable" }] },
    }]} /></MemoryRouter>);
    expect(screen.getByText("第 2 页：模型服务暂时不可用")).toBeInTheDocument();
    expect(screen.queryByText(/HTTP/)).not.toBeInTheDocument();
    expect(screen.queryByText(/题目生成/)).not.toBeInTheDocument();
  });

  it("shows accounting and actionable reasons without diagnostic identifiers", () => {
    render(
      <MemoryRouter>
        <SubmissionSourceOutcomePanel
          summary={{
            uploaded: 3,
            parsed: 1,
            failed: 1,
            identity_needs_review: 1,
            pending: 0,
          }}
          sources={sources}
          locale="zh-CN"
          taskId="task-1"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText(/3 份上传 = 1 份成功 \+ 1 份失败 \+ 1 份身份待确认/)).toBeInTheDocument();
    expect(screen.getByText("student-scan.png")).toBeInTheDocument();
    expect(screen.getByText("所选模型不支持图片输入")).toBeInTheDocument();
    expect(screen.queryByText(/job-21|src-ocr|provider_vision_not_supported/)).not.toBeInTheDocument();
    expect(screen.getByText("student-unknown.pdf")).toBeInTheDocument();
    expect(screen.getByText("学生身份需要教师确认")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "重新选择文件" })).toHaveAttribute(
      "href",
      "/tasks/task-1/submissions/upload",
    );
  });

  it("shows an identity conflict and unmatched question IDs at the same time", () => {
    render(
      <MemoryRouter>
        <SubmissionSourceOutcomePanel
          sources={[{
            source_id: "src-conflict",
            file_id: "file-conflict",
            file_name: "student-conflict.pdf",
            content_type: "application/pdf",
            size_bytes: 2048,
            status: "identity_needs_review",
            internal_status: "identity_conflict",
            reason_code: "student_identity_conflict",
            failure_phase: "identity",
            retryable: false,
            student_candidate: "S009",
            matched_answer_count: 1,
            unknown_question_ids: ["q7", "q9"],
            job_id: "job-22",
            attempt: 1,
            created_at: 1,
          }]}
          locale="zh-CN"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("学生身份发生冲突")).toBeInTheDocument();
    expect(screen.getByText("同时发现未匹配题号：q7、q9")).toBeInTheDocument();
  });
});

it.each(["zh-CN", "en-US"] as const)("renders older file records without content type (%s)", locale => {
  const { content_type: _contentType, ...olderSource } = sources[1];
  render(<MemoryRouter><SubmissionSourceOutcomePanel locale={locale} sources={[olderSource as SubmissionSourceOutcome]} /></MemoryRouter>);
  expect(screen.getByText("student-scan.png")).toBeVisible();
  expect(screen.getByText(locale === "zh-CN" ? /^文件 ·/ : /^File ·/)).toBeVisible();
  expect(screen.getByText(locale === "zh-CN" ? "所选模型不支持图片输入" : "The selected model does not support image input")).toBeVisible();
});

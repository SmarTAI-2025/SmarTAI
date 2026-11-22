import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
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

it("shows both required field repairs and optional confidence checks for one answer", () => {
  render(<MemoryRouter><SubmissionSourceOutcomePanel locale="zh-CN" sources={[{
    ...sources[1], student_candidate: "样例学生", answer_diagnostics: [{ q_id: "q1", number: "1.10",
      has_content: true, recognition_confidence: .4, flag: ["recognition_invalid_fields:content"] }],
  }]} /></MemoryRouter>);
  expect(screen.getByText("第 1.10 题")).toBeVisible();
  expect(screen.getByText(/需处理：待核对：作答内容/)).toHaveClass("text-red-700");
  expect(screen.getByText(/建议复核：作答识别置信度较低（0\.4）/)).toHaveClass("text-amber-700");
});

it("progress counts only severe responses, keeps twenty and hides yellow notes", () => {
  const answers = Array.from({ length: 25 }, (_, i) => ({ q_id: `q${i}`, number: String(i), has_content: true,
    recognition_confidence: .4, flag: ["recognition_invalid_fields:content"] }));
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale="zh-CN" sources={[{
    ...sources[1], answer_diagnostic_count: 100, answer_required_diagnostic_count: 25,
    answer_diagnostics: [{ q_id: "yellow", number: "yellow", has_content: true, recognition_confidence: .2, flag: [] }, ...answers],
  }]} /></MemoryRouter>);
  expect(screen.getByText("25 份作答需要关注")).toBeVisible();
  expect(screen.getByText("第 19 题")).toBeVisible();
  expect(screen.queryByText("第 20 题")).not.toBeInTheDocument();
  expect(screen.queryByText("第 yellow 题")).not.toBeInTheDocument();
  expect(screen.queryByText(/建议复核：/)).not.toBeInTheDocument();
  expect(screen.getByRole("list")).toHaveClass("max-h-[260px]", "overflow-y-auto");
});

it("shares the twenty-response preview limit across source files", () => {
  const answers = (prefix: string) => Array.from({ length: 15 }, (_, i) => ({ q_id: `${prefix}${i}`, number: `${prefix}${i}`,
    has_content: true, flag: ["parse_failed"] }));
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale="en-US" sources={[
    { ...sources[1], file_name: "a.txt", answer_diagnostics: answers("A"), answer_required_diagnostic_count: 15 },
    { ...sources[1], source_id: "second", file_name: "b.txt", answer_diagnostics: answers("B"), answer_required_diagnostic_count: 15 },
  ]} /></MemoryRouter>);
  expect(screen.getByText("Question A14")).toBeInTheDocument();
  expect(screen.getByText("Question B4")).toBeInTheDocument();
  expect(screen.queryByText("Question B5")).not.toBeInTheDocument();
});

describe("SubmissionSourceOutcomePanel", () => {
  it.each(["zh-CN", "en-US"] as const)("describes save-only manual resolution accurately in expanded diagnostics (%s)", async (locale) => {
    // The backend can resolve a paused source after saving valid answers and
    // repairing identity, without confirming the answer review.
    render(<MemoryRouter><SubmissionSourceOutcomePanel locale={locale} sources={[{
      ...sources[1], status: "parsed", reason_code: "operation_cancelled", recognition_reason_code: "operation_cancelled",
      resolution_status: "manual_resolved", failure_phase: "recognition", matched_answer_count: 1,
    }]} /></MemoryRouter>);
    const zh = locale === "zh-CN";
    const user = userEvent.setup();
    await user.click(screen.getByText(zh ? "查看每份文件" : "Review every file"));
    const diagnosticToggle = screen.getByText(zh ? "诊断信息" : "Diagnostic details");
    const diagnostics = diagnosticToggle.closest("details")!;
    expect(diagnostics).not.toHaveAttribute("open");
    await user.click(diagnosticToggle);
    expect(diagnostics).toHaveAttribute("open");
    expect(within(diagnostics).getByText(zh ? "教师已完成处理" : "Resolved by the teacher")).toBeVisible();
    expect(within(diagnostics).queryByText(/确认|confirmed/i)).not.toBeInTheDocument();
  });
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

it.each(["zh-CN", "en-US"] as const)("opens successful sources with required answers without asking for reupload (%s)", locale => {
  const zh = locale === "zh-CN";
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly taskId="task" locale={locale} sources={[{
    ...sources[0], answer_required_diagnostic_count: 2, answer_diagnostic_count: 2,
    answer_diagnostics: [1, 2].map(i => ({ q_id: `q${i}`, number: `1.${i}`, has_content: false, flag: ["no_matching_answer"] })),
  }]} /></MemoryRouter>);
  expect(screen.getByText(zh ? "第 1.1 题" : "Question 1.1").closest("details")).toHaveAttribute("open");
  expect(screen.getByText(zh ? /有 2 份作答需要处理/ : /2 responses need action/)).toBeVisible();
  expect(screen.queryByRole("link", { name: zh ? "重新选择文件" : "Choose files again" })).not.toBeInTheDocument();
});

it("keeps the required-answer count outside details in a mixed successful/failed batch", () => {
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale="zh-CN" sources={[
    sources[1], { ...sources[0], answer_required_diagnostic_count: 2, answer_diagnostics: [
      { q_id: "q1", number: "1", has_content: false, flag: ["no_matching_answer"] },
    ] },
  ]} /></MemoryRouter>);
  const notice = screen.getByText(/有 2 份作答需要处理/);
  expect(notice.closest("details")).toBeNull();
  expect(screen.getByText("第 1 题").closest("details")).toHaveAttribute("open");
});

it("uses the original diagnostics for the failed file explanation when advisory rows are hidden", () => {
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale="zh-CN" sources={[{
    ...sources[0], status: "failed", reason_code: "submission_parse_failed",
    page_diagnostics: { total_pages: 1, processed_pages: [1], failed_pages: [], unprocessed_pages: [], page_errors: [] },
    answer_required_diagnostic_count: 0, answer_diagnostics: [
      { q_id: "q1", number: "1.1.5", has_content: true, flag: ["unclear_text", "recognition_matching_advisory"] },
    ],
  }]} /></MemoryRouter>);
  expect(screen.getByText("页面已读完，部分作答仍需核对")).toBeVisible();
  expect(screen.getByText(/1\.1\.5 题的转写/)).toBeVisible();
  expect(screen.queryByText("第 1.1.5 题")).not.toBeInTheDocument();
});

it("renders both responses with the same conflicted question ID without duplicate-key errors", () => {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale="zh-CN" sources={[{
      ...sources[0], answer_required_diagnostic_count: 2, answer_diagnostic_count: 2,
      answer_diagnostics: [1, 2].map(() => ({ q_id: "q2", number: "2", has_content: true, flag: ["answer_assignment_conflict"] })),
    }]} /></MemoryRouter>);
    expect(screen.getAllByText("第 2 题")).toHaveLength(2);
    expect(screen.getByText(/有 2 份作答需要处理/)).toBeVisible();
    expect(errors).not.toHaveBeenCalled();
  } finally { errors.mockRestore(); }
});

it.each(["zh-CN", "en-US"] as const)("shows current conflicts after manual handoff instead of a resolved label (%s)", async (locale) => {
  const zh = locale === "zh-CN";
  render(<MemoryRouter><SubmissionSourceOutcomePanel requiredOnly locale={locale} sources={[{
    ...sources[0], status: "manual_review", resolution_status: "manual_review",
    reason_code: "operation_cancelled", recognition_reason_code: "operation_cancelled",
    answer_required_diagnostic_count: 2, answer_diagnostic_count: 2,
    answer_diagnostics: [1, 2].map(() => ({ q_id: "q2", number: "2", has_content: true, flag: ["answer_assignment_conflict"] })),
  }]} /></MemoryRouter>);
  expect(screen.getByText(zh ? /有 2 份作答需要处理/ : /2 responses need attention/)).toBeVisible();
  expect(screen.getAllByText(zh ? "第 2 题" : "Question 2")).toHaveLength(2);
  expect(screen.getByText(zh ? "识别未完成，等待人工处理" : "Recognition incomplete; awaiting manual review").closest("details")).toHaveAttribute("open");
  await userEvent.setup().click(screen.getByText(zh ? "诊断信息" : "Diagnostic details"));
  expect(screen.getByText(zh ? "等待人工处理" : "Awaiting manual review")).toBeVisible();
  expect(screen.queryByText(zh ? "教师已完成处理" : "Resolved by the teacher")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: zh ? "重新选择文件" : "Choose files again" })).not.toBeInTheDocument();
});

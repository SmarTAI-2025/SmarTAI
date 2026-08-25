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
  it("shows the accounting equation, exact reason, and diagnostic identifiers", () => {
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
    expect(screen.getByText("job-21:1:src-ocr")).toBeInTheDocument();
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

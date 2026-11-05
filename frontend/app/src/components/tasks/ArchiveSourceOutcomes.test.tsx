import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import type { SubmissionSourceOutcome } from "@/types";
import { SubmissionSourceOutcomePanel } from "./SubmissionSourceOutcomePanel";

const cases = [
  ["submission_archive_directories_only", "只找到目录，没有学生文件", "folders but no student files", "普通多层文件夹可以读取", "Ordinary nested folders are supported"],
  ["submission_archive_nested_unsupported", "压缩包内仍是压缩包", "another archive", "请先解压内层压缩包", "Unpack the inner archive first"],
] as const;

describe("archive diagnostics", () => {
  it.each(cases)("renders detected contents and recovery for %s in both languages", (code, zhTitle, enTitle, zhHelp, enHelp) => {
    for (const locale of ["zh-CN", "en-US"] as const) {
      const source: SubmissionSourceOutcome = {
        source_id: "src-archive", file_id: "file-archive", file_name: "folder/archive.zip",
        content_type: "application/zip", size_bytes: 128,
        status: "failed", internal_status: "parse_failed", reason_code: code,
        failure_phase: "archive", retryable: false, matched_answer_count: 0,
        unknown_question_ids: [], job_id: "job", attempt: 1, created_at: 1,
      };
      const view = render(<MemoryRouter><SubmissionSourceOutcomePanel locale={locale} sources={[source]} /></MemoryRouter>);
      expect(screen.getByText(new RegExp(locale === "zh-CN" ? zhTitle : enTitle))).toBeVisible();
      expect(screen.getByText(new RegExp(locale === "zh-CN" ? zhHelp : enHelp))).toBeVisible();
      expect(screen.queryByText(/请稍后重试|try again later/i)).not.toBeInTheDocument();
      view.unmount();
      const error = classifyRecoverableError(code, { locale });
      expect(error.actionKind).toBe("reupload");
      expect(error.description).toContain(locale === "zh-CN" ? "重新打包上传" : "repack");
      expect(error.description).toContain(locale === "zh-CN" ? "普通" : "Ordinary");
    }
  });
});

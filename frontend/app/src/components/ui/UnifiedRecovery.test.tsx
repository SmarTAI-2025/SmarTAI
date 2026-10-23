import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { RecoverableActionState } from "./RecoverableActionState";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { questionIssueNeedsReview, recognitionIssueLabel } from "@/lib/reviewConfirmation";
import { normalizeMarkdownMathInput } from "./MarkdownMath";
import type { PreparationIssue } from "@/types";

it.each(["provider_rate_limited", "provider_daily_quota_exceeded"])("renders truthful recovery controls for %s", code => {
  render(<MemoryRouter><RecoverableActionState info={classifyRecoverableError(code)}
    workflowRecovery={{ retry: { onClick: vi.fn() }, configurationHref: "/tasks/T/submissions/upload" }} /></MemoryRouter>);
  expect(screen.getByRole("link", { name: "返回修改配置" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "OpenAI" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "查看用量" })).not.toBeInTheDocument();
  if (code.includes("daily")) {
    expect(screen.queryByRole("button", { name: "重试失败项" })).not.toBeInTheDocument();
    expect(screen.getByText(/本轮已停止/)).toBeInTheDocument();
  } else {
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeEnabled();
    expect(screen.getByText(/模型请求受限/)).toBeInTheDocument();
  }
});

it("distinguishes unverified targets from actual missing source evidence", () => {
  const issue: PreparationIssue = { issue_id: "i", field: "stem", code: "recognition_partial", severity: "warning", status: "open",
    details: { coverage: { unverified_targets: ["1.1.5"] } } };
  expect(questionIssueNeedsReview(issue)).toBe(false);
  expect(recognitionIssueLabel(issue, "zh-CN")).toBe("请对照原文件核对题干。");
  issue.details = { coverage: { failed_pages: [2, 3], missing_targets: ["1.1.7"] } };
  expect(questionIssueNeedsReview(issue)).toBe(true);
  expect(recognitionIssueLabel(issue, "zh-CN")).toBe("识别失败的页码：2, 3；未找到的题号：1.1.7");
  expect(questionIssueNeedsReview({ ...issue, severity: "blocking" })).toBe(true);
});

it("renders prose newlines before lowercase and math, preserving code escapes and TeX", () => {
  expect(normalizeMarkdownMathInput(String.raw`first\nsecond\n$x \neq y$`)).toBe("first\nsecond\n$x \\neq y$");
  const code = '```python\nprint("\\n")\n```\n`\\n` and $\\nu+\\nabla f$';
  expect(normalizeMarkdownMathInput(code)).toBe(code);
  const math = String.raw`$x \nrightarrow y$, $x \neqslant y$`;
  expect(normalizeMarkdownMathInput(math)).toBe(math);
});

it.each(["zh-CN", "en-US"] as const)("hides internal diagnostics without changing stopped-run recovery (%s)", locale => {
  const retry = vi.fn();
  const info = classifyRecoverableError("operation_cancelled", { locale, jobId: "job-private-123", phase: "internal_phase" });
  info.technicalDetails.push({ label: locale === "zh-CN" ? "页数上限" : "Page limit", value: "200" });
  const { container } = render(<MemoryRouter><RecoverableActionState locale={locale} info={info}
    workflowRecovery={{ retry: { onClick: retry }, configurationHref: "/tasks/T/grading-setup" }} /></MemoryRouter>);
  expect(container).not.toHaveTextContent(/operation_cancelled|job-private-123|internal_phase/);
  expect(screen.getByText("200")).toBeInTheDocument();
  screen.getByRole("button", { name: info.actionLabel }).click();
  expect(retry).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("link", { name: locale === "zh-CN" ? "返回修改配置" : "Back to edit settings" })).toHaveAttribute("href", "/tasks/T/grading-setup");
  expect(info.technicalDetails.some(item => item.value === "operation_cancelled")).toBe(true);
});

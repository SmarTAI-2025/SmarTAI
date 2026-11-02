import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { RecoverableActionState } from "./RecoverableActionState";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { questionIssueNeedsReview, recognitionIssueLabel } from "@/lib/reviewConfirmation";
import { normalizeMarkdownMathInput } from "./MarkdownMath";
import type { PreparationIssue } from "@/types";

it.each(["provider_rate_limited", "provider_daily_quota_exceeded", "shared_pool_daily_limit_reached"])("renders truthful recovery controls for %s", code => {
  const retry = vi.fn();
  render(<MemoryRouter><RecoverableActionState info={classifyRecoverableError(code)}
    workflowRecovery={{ retry: { onClick: retry }, configurationHref: "/tasks/T/submissions/upload" }} /></MemoryRouter>);
  expect(screen.getByRole("link", { name: "返回修改配置" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "OpenAI" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "查看用量" })).not.toBeInTheDocument();
  if (code === "provider_daily_quota_exceeded") {
    expect(screen.getByText(/自动重试已停止/)).toBeInTheDocument();
  } else if (code === "provider_rate_limited") {
    expect(screen.getByText(/当前信息不足以确定是分钟限制还是日额度/)).toBeInTheDocument();
  }
  const button = screen.getByRole("button", { name: "重试失败项" });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  fireEvent.click(button);
  expect(retry).toHaveBeenCalledTimes(2);
});

it("distinguishes unverified targets from actual missing source evidence", () => {
  const issue: PreparationIssue = { issue_id: "i", field: "stem", code: "recognition_partial", severity: "warning", status: "open",
    details: { coverage: { unverified_targets: ["1.1.5"] } } };
  expect(questionIssueNeedsReview(issue)).toBe(false);
  expect(recognitionIssueLabel(issue, "zh-CN")).toContain("尚未发现具体缺失");
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

it.each(["zh-CN", "en-US"] as const)("distinguishes an explicit zero ceiling without hiding retry (%s)", locale => {
  const retry = vi.fn();
  const info = classifyRecoverableError("provider_zero_quota", { locale });
  const { container } = render(<MemoryRouter><RecoverableActionState locale={locale} info={info}
    workflowRecovery={{ retry: { onClick: retry }, configurationHref: "/tasks/T/grading-setup" }} /></MemoryRouter>);
  expect(container).toHaveTextContent(locale === "zh-CN" ? "当前无可用配额" : "No quota is currently available");
  expect(container).toHaveTextContent(locale === "zh-CN" ? "配额为0" : "zero quota limit");
  expect(container).not.toHaveTextContent(/已用完|耗尽|exhausted|midnight|跨天/);
  expect(info.dailyQuotaExhausted).toBeFalsy();
  const button = screen.getByRole("button", { name: locale === "zh-CN" ? "重试失败项" : "Retry failed items" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(retry).toHaveBeenCalledTimes(2);
});

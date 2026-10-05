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
    expect(screen.getByText(/不会自动跨天等待/)).toBeInTheDocument();
  } else {
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeEnabled();
    expect(screen.getByText(/当前信息不足以确定是分钟限制还是日额度/)).toBeInTheDocument();
  }
});

it("distinguishes routine confirmation from concrete missing pages", () => {
  const issue: PreparationIssue = { issue_id: "i", field: "stem", code: "recognition_partial", severity: "warning", status: "open",
    details: { coverage: { unverified_targets: ["1.1.5"] } } };
  expect(questionIssueNeedsReview(issue)).toBe(false);
  expect(recognitionIssueLabel(issue, "zh-CN")).toContain("尚未发现具体缺失");
  issue.details = { coverage: { failed_pages: [2, 3], missing_targets: ["1.1.7"] } };
  expect(questionIssueNeedsReview(issue)).toBe(true);
  expect(recognitionIssueLabel(issue, "zh-CN")).toBe("识别失败的页码：2, 3；未找到的题号：1.1.7");
});

it("renders prose newlines before lowercase and math, preserving code escapes and TeX", () => {
  expect(normalizeMarkdownMathInput(String.raw`first\nsecond\n$x \neq y$`)).toBe("first\nsecond\n$x \\neq y$");
  const code = '```python\nprint("\\n")\n```\n`\\n` and $\\nu+\\nabla f$';
  expect(normalizeMarkdownMathInput(code)).toBe(code);
  const math = String.raw`$x \nrightarrow y$, $x \neqslant y$`;
  expect(normalizeMarkdownMathInput(math)).toBe(math);
});

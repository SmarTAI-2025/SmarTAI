import { describe, expect, it } from "vitest";
import type { TaskResultResponse } from "@/types";
import { hideFrontierDemoDiagnostics, sanitizeFrontierDemoTaskResult } from "./frontierDemoFeedback";

describe("frontierDemoFeedback", () => {
  it("removes only the disabled SymPy diagnostic from Demo feedback", () => {
    expect(hideFrontierDemoDiagnostics(
      "The substitution is correct.\n\n（SymPy 验证：未启用 — 学生答案表达式无法解析；本评分基于 AI 推理）",
    )).toBe("The substitution is correct.");
    expect(hideFrontierDemoDiagnostics("（SymPy 验证：✓ 与标答一致）")).toBe("（SymPy 验证：✓ 与标答一致）");
  });

  it("leaves formal-product results untouched when Demo filtering is disabled", () => {
    const result = resultWithDiagnostic();
    expect(sanitizeFrontierDemoTaskResult(result, false)).toBe(result);
    expect(sanitizeFrontierDemoTaskResult(result, true)?.results?.[0].corrections[0].comment)
      .toBe("Reasoning remains visible.");
  });
});

function resultWithDiagnostic(): TaskResultResponse {
  return {
    status: "completed",
    task_id: "asg_demo",
    results: [{
      student_id: "student-1",
      corrections: [{
        q_id: "q1",
        type: "calculation",
        score: 4,
        max_score: 5,
        confidence: 0.8,
        comment: "Reasoning remains visible.\n\n（SymPy 验证：未启用 — 学生答案表达式无法解析；本评分基于 AI 推理）",
        steps: [],
        expert_results: [],
        requires_human_review: false,
        review_reasons: [],
      }],
    }],
  };
}

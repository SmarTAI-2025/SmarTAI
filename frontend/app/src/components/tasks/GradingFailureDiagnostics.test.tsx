import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { Correction, JobProgress, Task } from "@/types";
import { GradingFailureDiagnostics } from "./GradingFailureDiagnostics";

it("shows the affected student, question and specific failure before entering review", () => {
  const correction = { student_id: "s1", q_id: "q1", type: "concept", score: null, max_score: 10, confidence: 0,
    comment: "", steps: [], requires_human_review: true, review_status: "pending",
    review_reasons: ["all_failed", "parse_failed"],
    expert_results: [{ provider: "Gemini", score: 0, max_score: 10, confidence: 0, error_kind: "parse_failed", comment: "", steps: [] }],
  } as Correction;
  render(<GradingFailureDiagnostics locale="zh-CN" task={{ student_data: { s1: { stu_name: "测试教师的样例学生" } },
    problem_data: { q1: { number: "1.10" } } } as unknown as Task}
    progress={{ grading_review: [correction], grading_review_summary: { affected: 25, missing_scores: 3 } } as JobProgress} />);
  expect(screen.getByText("测试教师的样例学生 · 第 1.10 题")).toBeVisible();
  expect(screen.getByText(/25 份作答需要关注，其中 3 份缺少有效分数/)).toBeVisible();
  expect(screen.getByText(/Gemini：模型返回的评分格式无法读取/)).toHaveClass("text-red-700");
  expect(screen.queryByText(/所有专家均失败/)).not.toBeInTheDocument();
  expect(screen.getByText(/进入复核可查看全部/)).toBeVisible();
});

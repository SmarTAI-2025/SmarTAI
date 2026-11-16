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
  render(<GradingFailureDiagnostics locale="zh-CN" task={{ student_data: { s1: { stu_name: "测试教师的样例学生", stu_id: "S2026001" } },
    problem_data: { q1: { number: "1.10" } } } as unknown as Task}
    progress={{ grading_review: [correction], grading_review_summary: { affected: 25, missing_scores: 3 } } as JobProgress} />);
  expect(screen.getByText("测试教师的样例学生（S2026001） · 第 1.10 题")).toBeVisible();
  expect(screen.getByText(/25 份作答需要关注，其中 3 份缺少有效分数/)).toBeVisible();
  expect(screen.getByText(/Gemini：模型返回的评分格式无法读取/)).toHaveClass("text-red-700");
  expect(screen.queryByText(/所有专家均失败/)).not.toBeInTheDocument();
  expect(screen.getByText(/进入复核可查看全部/)).toBeVisible();
});

it("filters advisory notes and rows before limiting to twenty required responses", () => {
  const warning = { student_id: "advisory", q_id: "q0", score: 8, max_score: 10, confidence: .3,
    expert_results: [], review_reasons: ["low_confidence"], requires_human_review: true } as unknown as Correction;
  const blocked = Array.from({ length: 25 }, (_, i) => ({ ...warning, student_id: `required-${i}`, q_id: `q${i + 1}`, score: null }));
  render(<GradingFailureDiagnostics locale="en-US" progress={{ grading_review: [warning, ...blocked] } as JobProgress} />);
  expect(screen.getByText("25 responses need attention; 25 have no valid score")).toBeVisible();
  expect(screen.getAllByRole("listitem")).toHaveLength(20);
  expect(screen.queryByText(/Suggested:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/advisory ·/)).not.toBeInTheDocument();
  expect(screen.getByRole("list")).toHaveClass("max-h-48", "overflow-y-auto");
});

it("retains mandatory programming confirmation even when a score exists", () => {
  render(<GradingFailureDiagnostics locale="zh-CN" progress={{ grading_review: [{ student_id: "s1", q_id: "q1",
    score: 8, max_score: 10, confidence: 1, review_reasons: ["programming_ai_only"], expert_results: [] }] } as unknown as JobProgress} />);
  expect(screen.getByText("有 1 份作答需要关注，其中 0 份缺少有效分数")).toBeVisible();
  expect(screen.getByText(/需处理：/)).toBeVisible();
});

it("uses a readable fallback while student identity is unavailable", () => {
  render(<GradingFailureDiagnostics locale="zh-CN" progress={{ grading_review: [{
    student_id: "imported_f2036c18adb59b0b", q_id: "q1", score: null, max_score: 10,
    expert_results: [], review_reasons: ["missing_correction"],
  }] } as unknown as JobProgress} />);
  expect(screen.getByText("身份待确认 · 第 q1 题")).toBeVisible();
  expect(screen.queryByText(/imported_/)).not.toBeInTheDocument();
});

it("keeps confirmed display identities whose actual student number starts with imported_", () => {
  render(<GradingFailureDiagnostics locale="zh-CN" task={{ student_data: {
    imported_real: { stu_id: "imported_real", stu_name: "样例姓名", identity_status: "matched" },
  }, problem_data: { q1: { number: "1.2.3" } } } as unknown as Task}
    progress={{ grading_review: [{ student_id: "imported_real", q_id: "q1", score: null, max_score: 10, expert_results: [] }] } as unknown as JobProgress} />);
  expect(screen.getByText("样例姓名（imported_real） · 第 1.2.3 题")).toBeVisible();
});

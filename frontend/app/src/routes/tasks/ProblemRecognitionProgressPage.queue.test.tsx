import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
const state = vi.hoisted(() => ({ locale: "zh-CN", operation: "pending" }));
vi.mock("@/api/hooks", () => ({
  useStageProviders: () => ({ data: [] }),
  useRetryQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useTask: () => ({ data: { task_id: "queued", status: "extracting_problems" }, refetch: vi.fn() }),
}));
vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({ data: { status: "extracting_problems", active_job_id: "job", active_operation_status: state.operation }, progress: null, percent: 0, refetch: vi.fn() }),
}));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: state.locale, t: (key: string) => key }) }));
function Page() { return <MemoryRouter initialEntries={["/tasks/queued/problems/progress"]}><Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes></MemoryRouter>; }
it.each(["zh-CN", "en-US"])("shows a saved queued task, then resumes normal progress (%s)", (locale) => {
  state.locale = locale; state.operation = "pending";
  const { rerender } = render(<Page />);
  expect(screen.getByRole("heading", { name: new RegExp(locale === "zh-CN" ? "等待后台处理" : "Waiting for a worker") })).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  expect(screen.getAllByText(new RegExp(locale === "zh-CN" ? "识别尚未开始" : "Recognition has not started")).length).toBeGreaterThan(0);
  state.operation = "running"; rerender(<Page />);
  expect(screen.queryByText(locale === "zh-CN" ? "排队中" : "Queued")).not.toBeInTheDocument();
});

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import { EMPTY_FILTER_INTENT } from "@/lib/taskFilterIntent";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";

// A stable task object is important: fresh data on each render would conceal
// a missing memo dependency by incidentally rebuilding the question rows.
const { task } = vi.hoisted(() => ({ task: {
  task_id: "task-1", status: "problems_ready", problem_data: {
    Q1: { q_id: "Q1", number: "Q1", stem: "First question", max_score: 10 },
    Q2: { q_id: "Q2", number: "Q2", stem: "Second question", max_score: 20 },
  },
} }));
vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));
vi.mock("@/api/hooks/tasks", () => ({ useTask: () => ({ isSuccess: true, isLoading: false, data: task }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));

function questionOrder() {
  return screen.getAllByRole("row").slice(1).map(row => within(row).getAllByRole("cell")[0].textContent);
}

describe("question overview model and header order", () => {
  it.each(["", "?ask_order=1"])("applies model order and both header directions from %s", async initialQuery => {
    vi.mocked(interpretFilterIntent).mockResolvedValue({
      ...EMPTY_FILTER_INTENT,
      execution: {
        recognized: true, kind: "questions", explanation: "把第二题排在第一题前面",
        data: { columns: [], rows: [] }, selection: { kind: "questions", ids: ["Q2", "Q1"] }, chart: null,
      },
    });
    render(<MemoryRouter initialEntries={[`/tasks/task-1/questions${initialQuery}`]}>
      <Routes><Route path="/tasks/:taskId/questions" element={<QuestionPreparationOverviewPage />} /></Routes>
    </MemoryRouter>);
    const input = screen.getByRole("textbox", { name: "Ask SmarTAI：题目资料" });
    fireEvent.change(input, { target: { value: "把第二题排在第一题前面" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(questionOrder()).toEqual(["Q2", "Q1"]));
    expect(screen.getByRole("columnheader", { name: "题号" })).toHaveAttribute("aria-sort", "none");
    expect(screen.getByRole("button", { name: "题号，当前未排序；点击升序" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /题号，/ }));
    expect(screen.getByRole("columnheader", { name: "题号" })).toHaveAttribute("aria-sort", "ascending");
    expect(questionOrder()).toEqual(["Q1", "Q2"]);
    fireEvent.click(screen.getByRole("button", { name: /题号，/ }));
    expect(questionOrder()).toEqual(["Q2", "Q1"]);

    fireEvent.click(screen.getByRole("button", { name: /题号，/ }));
    expect(questionOrder()).toEqual(["Q1", "Q2"]);
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(questionOrder()).toEqual(["Q2", "Q1"]));
  });
});

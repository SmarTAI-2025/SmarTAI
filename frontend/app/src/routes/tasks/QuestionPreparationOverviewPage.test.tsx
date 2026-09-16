import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FilterIntentResult } from "@/types";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";

const filterIntentMocks = vi.hoisted(() => ({ mutate: vi.fn(), reset: vi.fn() }));
const taskMocks = vi.hoisted(() => ({ includeLowConfidence: false }));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({
    isLoading: false,
    isSuccess: true,
    data: {
      task_id: "task-1",
      status: "problems_ready",
      problem_data: {
        Q1: {
          q_id: "Q1",
          number: "Q1",
          type: "概念题",
          stem: "三角函数基础",
          max_score: 10,
          max_score_source: "default_10",
          max_score_review_status: "needs_review",
          criterion: "说明基本概念",
          preparation_issues: [{
            issue_id: "score-risk-1",
            field: "max_score",
            code: "default_max_score_requires_review",
            severity: "warning",
            status: "open",
          }, ...(taskMocks.includeLowConfidence ? [{
            issue_id: "confidence-risk-1",
            field: "stem",
            code: "low_confidence" as const,
            severity: "warning",
            status: "open",
          }] : [])],
        },
        Q2: {
          q_id: "Q2",
          number: "Q2",
          type: "计算题",
          stem: "二次函数求值",
          max_score: 5,
          max_score_source: "teacher_edited",
          max_score_review_status: "confirmed",
          reference_answer: "代入并计算。",
          criterion: "列式和计算各给分。",
          preparation_issues: [],
        },
      },
    },
  }),
}));

vi.mock("@/api/hooks/analytics", () => ({
  useAnalyticsFilterIntent: () => ({
    isPending: false,
    isError: false,
    error: null,
    mutate: filterIntentMocks.mutate,
    reset: filterIntentMocks.reset,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN" }),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

function renderPage(initialEntry: string) {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/tasks/:taskId/questions"
          element={(
            <>
              <QuestionPreparationOverviewPage />
              <LocationProbe />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>,
  );
  return screen.getByRole("searchbox", { name: /向 SmarTAI 描述题目资料筛选条件/ }) as HTMLInputElement;
}

describe("QuestionPreparationOverviewPage smart search", () => {
  beforeEach(() => {
    taskMocks.includeLowConfidence = false;
    filterIntentMocks.mutate.mockReset();
    filterIntentMocks.reset.mockReset();
  });

  it("shows each maximum score and the total while flagging defaults", () => {
    renderPage("/tasks/task-1/questions");

    expect(screen.getByRole("columnheader", { name: "满分" })).toBeInTheDocument();
    expect(screen.getByTitle("系统默认，需确认")).toHaveTextContent("10 分");
    expect(screen.getByText(/作业总分 15/)).toBeInTheDocument();
    expect(screen.getByTitle("当前使用默认 10 分，请确认题目满分")).toBeInTheDocument();
  });

  it("waits for Apply before committing a composing Ask query", async () => {
    const input = renderPage("/tasks/task-1/questions?status=open");

    fireEvent.input(input, {
      target: { value: "san" },
      isComposing: true,
      inputType: "insertCompositionText",
    });

    expect(input).toHaveValue("san");
    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=open");
    expect(screen.getByRole("row", { name: /Q1/ })).toBeInTheDocument();

    fireEvent.compositionEnd(input, { data: "三" });
    fireEvent.change(input, { target: { value: "三" } });

    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=open");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("status=open&q=%E4%B8%89");
    });
  });

  it("keeps the caret before the Chinese character across repeated Ask-input deletions", async () => {
    const user = userEvent.setup();
    const input = renderPage("/tasks/task-1/questions?q=ssasan%E4%B8%89");
    input.focus();
    input.setSelectionRange(6, 6);

    const edits = [
      ["ssasa三", 5],
      ["ssas三", 4],
      ["ssa三", 3],
      ["ss三", 2],
      ["s三", 1],
      ["三", 0],
    ] as const;

    for (const [value, caret] of edits) {
      await user.keyboard("{Backspace}");
      expect(input).toHaveValue(value);
      expect(input.selectionStart).toBe(caret);
      expect(input.selectionEnd).toBe(caret);
      expect(input).toHaveFocus();
    }
  });

  it("interprets maximum-score sorting locally and reverses it from the header", async () => {
    const input = renderPage("/tasks/task-1/questions");
    fireEvent.change(input, { target: { value: "按满分升序" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      const rows = screen.getAllByRole("row").slice(1);
      expect(rows[0]).toHaveTextContent("Q2");
      expect(rows[1]).toHaveTextContent("Q1");
      expect(screen.getByText("本地规则已识别 · 未调用模型")).toBeInTheDocument();
    });

    const sort = screen.getByRole("button", { name: "按满分排序" });
    fireEvent.click(sort);
    fireEvent.click(sort);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Q1");
    expect(rows[1]).toHaveTextContent("Q2");
  });

  it("applies a structured low-confidence intent without a preparation status", async () => {
    taskMocks.includeLowConfidence = true;
    const intent: FilterIntentResult = {
      recognized: true,
      min_score_percent: null,
      max_score_percent: null,
      pass_status: null,
      low_confidence: true,
      review_status: null,
      disagreement: false,
      annotated: false,
      sort: null,
      question_tokens: [],
      question_types: [],
      max_average_confidence: null,
      missing_knowledge: false,
      min_max_score: null,
      max_max_score: null,
      preparation_status: null,
      material_field: null,
      material_status: null,
      submission_status: null,
      text_terms: [],
      explanation: "只显示低置信题目。",
    };
    filterIntentMocks.mutate.mockImplementation((_input, options: { onSuccess?: (result: FilterIntentResult) => void }) => {
      window.setTimeout(() => options.onSuccess?.(intent), 0);
    });

    const input = renderPage("/tasks/task-1/questions");
    fireEvent.change(input, { target: { value: "请找出阅读体验较差的题目" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      const rows = screen.getAllByRole("row").slice(1);
      expect(rows).toHaveLength(1);
      expect(rows[0]).toHaveTextContent("Q1");
    });
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";

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
          criterion: "说明基本概念",
          preparation_issues: [],
        },
      },
    },
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
  return screen.getByRole("textbox", { name: "SmarTAI 智能筛选题目资料" }) as HTMLInputElement;
}

describe("QuestionPreparationOverviewPage smart search", () => {
  it("does not apply a native composing input event before composition ends", async () => {
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

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("status=open&q=%E4%B8%89");
    });
  });

  it("keeps the caret before the Chinese character across repeated deletions", async () => {
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
      await waitFor(() => {
        expect(screen.getByTestId("location-search")).toHaveTextContent(`q=${encodeURIComponent(value)}`);
      });
      expect(input).toHaveValue(value);
      expect(input.selectionStart).toBe(caret);
      expect(input.selectionEnd).toBe(caret);
      expect(input).toHaveFocus();
    }
  });
});

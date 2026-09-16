import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

const taskState = vi.hoisted(() => ({
  current: {
    isLoading: true,
    isError: false,
    isSuccess: false,
    data: undefined as unknown,
    refetch: vi.fn(),
  },
}));

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => taskState.current,
}));

vi.mock("@/api/hooks/analytics", () => ({
  useAnalyticsFilterIntent: () => ({
    isPending: false,
    isError: false,
    error: null,
    mutate: vi.fn(),
    reset: vi.fn(),
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
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
          path="/tasks/:taskId/submissions"
          element={(
            <>
              <SubmissionReviewOverviewPage />
              <LocationProbe />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>,
  );
  return screen.getByRole("searchbox", { name: /向 SmarTAI 描述筛选条件/ }) as HTMLInputElement;
}

function sortableTask() {
  return {
    task_id: "task-1",
    name: "Submission review",
    owner_id: "teacher-1",
    status: "submissions_ready",
    workflow_revision: 1,
    problem_count: 2,
    student_count: 2,
    kb_docs: {},
    kb_doc_count: 0,
    created_at: 1,
    updated_at: 1,
    problem_data: {
      Q1: { q_id: "Q1", number: "1", type: "Calculation", stem: "First question", review_status: "confirmed" },
      Q2: { q_id: "Q2", number: "2", type: "Calculation", stem: "Second question", review_status: "confirmed" },
    },
    student_data: {
      "S-1": {
        stu_id: "S-1",
        stu_name: "Missing first",
        stu_ans: [{ q_id: "Q2", number: "2", type: "Calculation", content: "Second answer", flag: [] }],
      },
      "S-2": {
        stu_id: "S-2",
        stu_name: "Recognized first",
        stu_ans: [
          { q_id: "Q1", number: "1", type: "Calculation", content: "First answer", flag: [] },
          { q_id: "Q2", number: "2", type: "Calculation", content: "Second answer", flag: [] },
        ],
      },
    },
  };
}

function matrixStudentIds() {
  return Array.from(document.querySelectorAll("tbody tr")).map((row) => row.querySelector("td")?.textContent?.trim());
}

describe("SubmissionReviewOverviewPage smart search", () => {
  beforeEach(() => {
    taskState.current = {
      isLoading: true,
      isError: false,
      isSuccess: false,
      data: undefined,
      refetch: vi.fn(),
    };
  });

  it("waits for Chinese IME composition to finish and applies the query only after submit", async () => {
    const input = renderPage("/tasks/task-1/submissions?status=review");

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "s" } });
    fireEvent.change(input, { target: { value: "sa" } });
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.change(input, { target: { value: "三" } });

    expect(input).toHaveValue("三");
    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=review");

    fireEvent.compositionEnd(input);
    fireEvent.blur(input);
    fireEvent.change(screen.getByRole("combobox", { name: "submissionReviewStatusLabel" }), {
      target: { value: "missing" },
    });

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("status=missing");
    });
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("q=%E4%B8%89");

    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("q=%E4%B8%89");
    });
  });

  it("keeps the caret before the Chinese character across repeated Ask-input deletions", async () => {
    const user = userEvent.setup();
    const input = renderPage("/tasks/task-1/submissions?q=ssasan%E4%B8%89");
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

    expect(screen.getByTestId("location-search")).toHaveTextContent("q=ssasan%E4%B8%89");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("q=%E4%B8%89");
    });
  });

  it("sorts the question column from its header and exposes the active direction", async () => {
    const user = userEvent.setup();
    taskState.current = {
      isLoading: false,
      isError: false,
      isSuccess: true,
      data: sortableTask(),
      refetch: vi.fn(),
    };
    renderPage("/tasks/task-1/submissions");

    const initialHeader = (await screen.findAllByRole("columnheader"))[2];
    await user.click(within(initialHeader).getByRole("button"));

    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("sort=question%3AQ1%3Aasc"));
    expect(screen.getAllByRole("columnheader")[2]).toHaveAttribute("aria-sort", "ascending");
    expect(matrixStudentIds()).toEqual(["S-2", "S-1"]);

    await user.click(within(screen.getAllByRole("columnheader")[2]).getByRole("button"));

    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("sort=question%3AQ1%3Adesc"));
    expect(screen.getAllByRole("columnheader")[2]).toHaveAttribute("aria-sort", "descending");
    expect(matrixStudentIds()).toEqual(["S-1", "S-2"]);
  });
});

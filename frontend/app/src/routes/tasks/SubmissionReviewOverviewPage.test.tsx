import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ isLoading: true, isSuccess: false }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ t: (key: string) => key }),
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
  return screen.getByRole("searchbox", { name: "submissionReviewSearchLabel" }) as HTMLInputElement;
}

describe("SubmissionReviewOverviewPage smart search", () => {
  it("waits for Chinese IME composition to finish before updating the URL filter", async () => {
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
      expect(screen.getByTestId("location-search")).toHaveTextContent("q=%E4%B8%89");
    });
  });

  it("keeps the caret before the Chinese character across repeated deletions", async () => {
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

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NewTaskStepper } from "./NewTaskStepper";

vi.mock("@/api/hooks/tasks", () => ({
  useTask: vi.fn(),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", t: (key: string) => key }),
}));

const { useTask } = await import("@/api/hooks/tasks");

function renderStepper(props: React.ComponentProps<typeof NewTaskStepper>) {
  return render(
    <MemoryRouter initialEntries={["/tasks/task-1/review"]}>
      <Routes>
        <Route path="/tasks/:taskId/review" element={<NewTaskStepper {...props} />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("NewTaskStepper workflow guidance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useTask as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { task_id: "task-1", status: "graded" },
    });
  });

  it("opens read-only Results Analysis after grading even before teacher confirmation", () => {
    const onLockedStepActivate = vi.fn();

    renderStepper({
      currentStep: 6,
      lockedStep: 7,
      lockedStepReason: "One response still needs confirmation.",
      onLockedStepActivate,
    });

    expect(screen.getByRole("link", { name: "newTaskStepComplete" })).toHaveAttribute(
      "href",
      "/tasks/task-1/results",
    );
    expect(onLockedStepActivate).not.toHaveBeenCalled();
  });

  it("keeps each connector in normal flow after its step label", () => {
    renderStepper({ currentStep: 1, reachableStep: 1 });

    const uploadLabel = screen.getByText("newTaskStepUpload");
    const step = uploadLabel.closest("li");
    const connector = step?.querySelector<HTMLElement>("[data-step-connector]");

    expect(connector).toBeInTheDocument();
    expect(connector).not.toHaveClass("absolute");
    expect(connector).toHaveClass("mx-1.5", "min-w-2", "flex-1");
    expect(connector?.previousElementSibling).toContainElement(uploadLabel);
  });

  it("uses concise English labels below the wide-screen breakpoint while preserving full accessible names", () => {
    renderStepper({ currentStep: 1, reachableStep: 1 });

    expect(screen.getByText("Questions")).toHaveClass("xl:hidden");
    expect(screen.getByText("newTaskStepUpload")).toHaveClass("hidden", "xl:inline");
    expect(screen.getByRole("link", { name: "newTaskStepUpload" })).toHaveAttribute(
      "title",
      "newTaskStepUpload",
    );
    expect(screen.getByText("Analysis")).toHaveClass("xl:hidden");
  });

  it("returns completed tasks to editable grading setup from the Grading step", () => {
    renderStepper({ currentStep: 6 });

    expect(screen.getByRole("link", { name: "newTaskStepGrading" })).toHaveAttribute(
      "href",
      "/tasks/task-1/grading-setup",
    );
  });

  it("greys stale later steps after the server rewinds to question review", () => {
    (useTask as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        status: "problems_ready",
        grading_setup_configured: false,
        problem_data: {
          q1: { q_id: "q1", review_status: "needs_review" },
        },
      },
    });

    renderStepper({ currentStep: 6 });

    expect(screen.queryByRole("link", { name: "newTaskStepSubmissions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "newTaskStepGrading" })).not.toBeInTheDocument();
    expect(screen.getByText("newTaskStepGrading").closest("div")).toHaveAttribute("aria-disabled", "true");
  });
});

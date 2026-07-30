import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

describe("NewTaskStepper locked-step guidance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useTask as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { task_id: "task-1", status: "graded" },
    });
  });

  it("explains a locked Results Analysis step and activates its recovery action", async () => {
    const user = userEvent.setup();
    const onLockedStepActivate = vi.fn();

    renderStepper({
      currentStep: 6,
      lockedStep: 7,
      lockedStepReason: "One response still needs confirmation.",
      onLockedStepActivate,
    });

    const lockedStep = screen.getByRole("button", {
      name: /newTaskStepComplete.*One response still needs confirmation/,
    });
    expect(lockedStep).toHaveAttribute("title", "One response still needs confirmation.");

    await user.click(lockedStep);

    expect(onLockedStepActivate).toHaveBeenCalledOnce();
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
});

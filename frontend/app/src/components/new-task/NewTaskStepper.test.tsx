import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NewTaskStepper } from "./NewTaskStepper";

vi.mock("@/api/hooks/tasks", () => ({
  useTask: vi.fn(),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ t: (key: string) => key }),
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
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddSubmissionsPage } from "./AddSubmissionsPage";

const mutateAsync = vi.fn();

vi.mock("@/api/hooks", () => ({
  useTask: () => ({
    data: {
      task_id: "task-1",
      status: "problems_ready",
      student_count: 0,
      submission_file_name: null,
    },
    isError: false,
    isLoading: false,
  }),
  useParseSubmissions: () => ({
    isPending: false,
    mutateAsync,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ t: (key: string) => key }),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/tasks/task-1/submissions/upload"]}>
      <Routes>
        <Route path="/tasks/:taskId/submissions/upload" element={<AddSubmissionsPage />} />
        <Route path="/tasks/:taskId/submissions/progress" element={<div>progress page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AddSubmissionsPage OCR uploads", () => {
  beforeEach(() => {
    mutateAsync.mockReset();
    mutateAsync.mockResolvedValue({ status: "started", task_id: "task-1" });
  });

  it("accepts a student image and sends it through the submission parsing mutation", async () => {
    const { container } = renderPage();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    expect(input.accept).toContain(".jpg");
    expect(input.accept).toContain(".jpeg");
    expect(input.accept).toContain(".png");
    expect(input.accept).toContain(".webp");

    const image = new File(["student answer"], "S003_Li_geography_notes.jpg", {
      type: "image/jpeg",
    });
    fireEvent.change(input, { target: { files: [image] } });
    fireEvent.click(screen.getByRole("button", { name: "submissionUploadStart" }));

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith(expect.objectContaining({
        taskId: "task-1",
        file: image,
        identityMode: "filename",
      }));
    });
    expect(await screen.findByText("progress page")).toBeInTheDocument();
  });
});

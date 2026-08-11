import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddSubmissionsPage } from "./AddSubmissionsPage";

const mutateAsync = vi.fn();
let taskName = "Ordinary assignment";

vi.mock("@/api/hooks", () => ({
  useCurrentUser: () => ({ data: { id: "teacher-1" } }),
  useTask: () => ({
    data: {
      task_id: "task-1",
      name: taskName,
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
  useI18n: () => ({ t: (key: string) => key, locale: "en-US" }),
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
    taskName = "Ordinary assignment";
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

  it("offers only the four named preset fixtures and a direct route back to the same Live Demo task", () => {
    taskName = "SmarTAI Live Demo · 2026-08-11";

    const { container } = renderPage();

    expect(screen.getByRole("heading", { name: "Four samples are ready for this task" })).toBeInTheDocument();
    expect(screen.getByText("Alex Chen")).toBeInTheDocument();
    expect(screen.getByText("Maya Lin")).toBeInTheDocument();
    expect(screen.getByText("Jordan Rivera")).toBeInTheDocument();
    expect(screen.getByText("Taylor Singh")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Continue with Demo samples/i })).toHaveAttribute(
      "href",
      "/frontier/live?taskId=task-1",
    );
    expect(container.querySelector('input[type="file"]')).not.toBeInTheDocument();
    expect(screen.queryByText("submissionUploadIdentityTitle")).not.toBeInTheDocument();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";

const retryMutateAsync = vi.fn();
const refetchTask = vi.fn();
const refetchProgress = vi.fn();
const task = {
  task_id: "question-task",
  status: "error",
  workflow_revision: 8,
  last_failed_job_id: "failed-question-job",
  question_recognition_provider_id: "provider-old",
  error: "provider_vision_not_supported",
};

vi.mock("@/api/hooks", () => ({
  useExperts: () => ({
    data: [
      {
        provider_id: "provider-old",
        provider_type: "openai",
        model: "text-only",
        enabled: true,
        is_default: false,
      },
      {
        provider_id: "provider-new",
        provider_type: "gemini",
        model: "vision-model",
        enabled: true,
        is_default: true,
      },
    ],
    isError: false,
    isLoading: false,
  }),
  useRetryQuestionPreparation: () => ({
    isPending: false,
    mutateAsync: retryMutateAsync,
  }),
  useTask: () => ({
    data: task,
    error: null,
    isFetching: false,
    refetch: refetchTask,
  }),
}));

vi.mock("@/hooks/useTaskProgress", () => ({
  useTaskProgress: () => ({
    data: { status: "error" },
    error: null,
    isFetching: false,
    progress: {
      error_detail: new APIError(422, "vision rejected", {
        detail: { code: "provider_vision_not_supported" },
      }),
      messages: [],
    },
    refetch: refetchProgress,
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", t: (key: string) => key }),
}));

describe("ProblemRecognitionProgressPage recovery", () => {
  beforeEach(() => {
    retryMutateAsync.mockReset();
    refetchTask.mockReset();
    refetchProgress.mockReset();
    retryMutateAsync.mockResolvedValue({ status: "started", job_id: "retry-job" });
  });

  it("switches models and retries the failed stage without asking for an upload", async () => {
    render(
      <MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}>
        <Routes>
          <Route
            path="/tasks/:taskId/problems/progress"
            element={<ProblemRecognitionProgressPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    const select = screen.getByRole("combobox", { name: "题目识别模型" });
    expect(select).toHaveValue("provider-old");
    fireEvent.change(select, { target: { value: "provider-new" } });
    fireEvent.click(screen.getByRole("button", { name: "用所选模型重试" }));

    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledWith({
      taskId: "question-task",
      jobId: "failed-question-job",
      recognitionProviderId: "provider-new",
      expectedWorkflowRevision: 8,
    }));
  });
});

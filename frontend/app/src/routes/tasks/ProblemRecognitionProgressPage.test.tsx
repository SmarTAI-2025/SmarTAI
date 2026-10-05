import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { ProblemRecognitionProgressPage } from "./ProblemRecognitionProgressPage";
vi.mock("@/api/hooks/experts", () => ({ useProviderCatalog: () => ({ data: [] }) }));

const retryMutateAsync = vi.fn();
const manualMutateAsync = vi.fn();
let failedQuestions: string[] = [];
const refetchTask = vi.fn();
const refetchProgress = vi.fn();
const failedTask = {
  task_id: "question-task",
  status: "error",
  workflow_revision: 8,
  last_failed_job_id: "failed-question-job",
  question_recognition_provider_id: "provider-old",
  error: "provider_vision_not_supported",
};
let task = { ...failedTask };
let snapshot = { ...failedTask };
let isPolling = false;

vi.mock("@/api/hooks", () => ({
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: manualMutateAsync }),
  useStageProviders: () => ({
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
    data: snapshot,
    error: null,
    isFetching: isPolling,
    progress: {
      failed_question_ids: failedQuestions,
      error_detail: new APIError(422, "vision rejected", {
        detail: { code: snapshot.error },
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
    task = { ...failedTask };
    snapshot = { ...failedTask };
    isPolling = false;
    failedQuestions = [];
    manualMutateAsync.mockReset().mockResolvedValue({ first_question_id: "q2", workflow_revision: 9 });
    retryMutateAsync.mockReset();
    refetchTask.mockReset();
    refetchProgress.mockReset();
    retryMutateAsync.mockResolvedValue({ status: "started", job_id: "retry-job" });
  });

  it("keeps retry idle during background status polls and only retries on click", async () => {
    expect(manualMutateAsync).not.toHaveBeenCalled();
    const page = <MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}><Routes>
      <Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} />
    </Routes></MemoryRouter>;
    const { rerender } = render(page);
    isPolling = true;
    rerender(<MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}><Routes>
      <Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} />
    </Routes></MemoryRouter>);
    const retry = screen.getByRole("button", { name: "重试失败项" });
    expect(retry).toBeEnabled();
    expect(retry.querySelector(".animate-spin")).toBeNull();
    expect(screen.getByRole("button", { name: "problemProgressRefresh" }).querySelector(".animate-spin")).toBeNull();
    expect(retryMutateAsync).not.toHaveBeenCalled();
    fireEvent.click(retry);
    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledTimes(1));
  });

  it("offers explicit manual completion beside retry and opens the failed question editor", async () => {
    failedQuestions = ["q2"];
    render(<MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}><Routes>
      <Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} />
      <Route path="/tasks/:taskId/questions/q2/content" element={<div>Failed question editor</div>} />
    </Routes></MemoryRouter>);
    expect(screen.getByRole("button", { name: "重试失败项" })).toBeEnabled();
    expect(manualMutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "手动补全失败题目" }));
    expect(await screen.findByText("Failed question editor")).toBeInTheDocument();
    expect(manualMutateAsync).toHaveBeenCalledWith({ taskId: "question-task", jobId: "failed-question-job", expectedWorkflowRevision: 8 });
    expect(retryMutateAsync).not.toHaveBeenCalled();
  });

  it.each([false, true])("retries preserved sources with a frozen provider (stale detail: %s)", async (staleDetail) => {
    if (staleDetail) {
      task = { ...task, status: "extracting_problems", workflow_revision: 7, last_failed_job_id: "" };
    }
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
    expect(select).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));

    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledWith({
      taskId: "question-task",
      jobId: "failed-question-job",
      recognitionProviderId: "provider-old",
      expectedWorkflowRevision: 8,
    }));
  });

  it("requires explicit acknowledgement before restarting uncertain provider work", async () => {
    snapshot.error = "provider_submit_uncertain";
    render(<MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}><Routes>
      <Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} />
    </Routes></MemoryRouter>);
    const restart = screen.getByRole("button", { name: "重试失败项" });
    expect(restart).toBeDisabled();
    fireEvent.click(restart);
    expect(retryMutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(restart);
    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledWith(expect.objectContaining({
      jobId: "failed-question-job", acknowledgePossibleDuplicateCall: true,
    })));
  });
  it.each(["provider_auth_failed", "provider_request_rejected", "provider_rate_limited", "provider_timeout", "provider_upstream_unavailable", "source_empty", "unknown_failure"])("always exposes retry and edit settings for %s", async (code) => {
    snapshot.error = code;
    retryMutateAsync.mockImplementation(() => new Promise(() => {}));
    render(<MemoryRouter initialEntries={["/tasks/question-task/problems/progress"]}><Routes><Route path="/tasks/:taskId/problems/progress" element={<ProblemRecognitionProgressPage />} /></Routes></MemoryRouter>);
    expect(screen.getByRole("link", { name: "返回修改配置" })).toHaveAttribute("href", "/tasks/question-task/upload/problems");
    const retry = screen.getByRole("button", { name: "重试失败项" });
    fireEvent.change(screen.getByRole("combobox", { name: "题目识别模型" }), { target: { value: "provider-new" } });
    expect(retryMutateAsync).not.toHaveBeenCalled();
    fireEvent.click(retry); fireEvent.click(retry);
    await waitFor(() => expect(retryMutateAsync).toHaveBeenCalledTimes(1));
    expect(retryMutateAsync.mock.calls[0][0].recognitionProviderId).toBe("provider-new");
  });

});

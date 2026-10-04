import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as tasksApi from "@/api/tasks";
import { taskKeys } from "@/api/hooks/keys";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { Task } from "@/types";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";
import { SubmissionReviewOverviewPage } from "./SubmissionReviewOverviewPage";

vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
afterEach(() => vi.restoreAllMocks());

describe("recognition completion handoff", () => {
  it.each([
    ["questions", "extracting_problems", "problems_ready"],
    ["submissions", "parsing_submissions", "submissions_ready"],
  ] as const)("waits for fresh %s detail instead of bouncing back to progress", async (page, active, ready) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const previous = { task_id: "handoff", status: active, workflow_revision: 1, problem_data: {}, student_data: {} } as Task;
    client.setQueryData(taskKeys.detail("handoff"), previous);
    let finish!: (task: Task) => void;
    vi.spyOn(tasksApi, "getTask").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter initialEntries={[`/tasks/handoff/${page}`]}><Routes>
      <Route path="/tasks/:taskId/questions" element={<QuestionPreparationOverviewPage />} />
      <Route path="/tasks/:taskId/submissions" element={<SubmissionReviewOverviewPage />} />
      <Route path="/tasks/:taskId/problems/progress" element={<div>Unexpected progress redirect</div>} />
      <Route path="/tasks/:taskId/submissions/progress" element={<div>Unexpected progress redirect</div>} />
    </Routes></MemoryRouter></I18nProvider></QueryClientProvider>);
    await waitFor(() => expect(tasksApi.getTask).toHaveBeenCalledWith("handoff"));
    expect(screen.queryByText("Unexpected progress redirect")).not.toBeInTheDocument();
    await act(async () => finish({ ...previous, status: ready, workflow_revision: 2 }));
    await waitFor(() => expect(client.getQueryData<Task>(taskKeys.detail("handoff"))?.status).toBe(ready));
    expect(screen.queryByText("Unexpected progress redirect")).not.toBeInTheDocument();
    expect(screen.getAllByRole("heading").length).toBeGreaterThan(0);
    client.clear();
  });
});

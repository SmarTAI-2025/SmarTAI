import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { continueTaskRun } from "@/api/tasks";
import { TaskExecutionStatus } from "./TaskExecutionStatus";

const { state, refetch } = vi.hoisted(() => ({
  state: { current: { error: "operation_cancelled", last_failed_job_id: "job-1", active_job_id: null, workflow_revision: 4 } },
  refetch: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/api/tasks", () => ({ continueTaskRun: vi.fn() }));
vi.mock("@/hooks/useTaskProgress", () => ({ useTaskProgress: () => ({ data: state.current, refetch }) }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US" }) }));
const changed = vi.fn().mockResolvedValue(undefined);
const component = () => <MemoryRouter><TaskExecutionStatus taskId="task" jobId="job-1" onChanged={changed} /></MemoryRouter>;
beforeEach(() => {
  vi.clearAllMocks();
  state.current = { error: "operation_cancelled", last_failed_job_id: "job-1", active_job_id: null, workflow_revision: 4 };
  vi.mocked(continueTaskRun).mockResolvedValue({ status: "started", job_id: "new-job" });
});

it.each(["question_preparation_wall_time_limit_reached", "provider_submit_uncertain"])("requires explicit duplicate-billing consent before continuing %s", async code => {
  state.current.error = code;
  render(component());
  expect(screen.getByRole("link", { name: "Continue manually" })).toHaveAttribute("href", "/tasks/task/questions");
  const button = screen.getByRole("button", { name: "Continue processing" });
  expect(button).toBeDisabled();
  expect(continueTaskRun).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(continueTaskRun).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button).toBeEnabled();
  fireEvent.click(button);
  await waitFor(() => expect(continueTaskRun).toHaveBeenCalledExactlyOnceWith("task", "job-1", 4, false, true));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});

it("permits an explicit active-budget retry without unnecessary consent", async () => {
  state.current.error = "question_preparation_time_limit_reached";
  render(component());
  expect(screen.getByRole("status")).toHaveTextContent("active processing limit is 1.5 hours");
  expect(screen.getByRole("status")).toHaveTextContent("elapsed time limit after the first start is 2.5 hours");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(continueTaskRun).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Continue processing" }));
  await waitFor(() => expect(continueTaskRun).toHaveBeenCalledExactlyOnceWith("task", "job-1", 4, false, false));
});

it("requires consent if the server discovers an uncertain active-budget submission", async () => {
  state.current.error = "question_preparation_time_limit_reached";
  vi.mocked(continueTaskRun).mockRejectedValueOnce(new APIError(409, "uncertain", { detail: { code: "provider_submit_uncertain" } }));
  render(component());
  fireEvent.click(screen.getByRole("button", { name: "Continue processing" }));
  await screen.findByRole("checkbox");
  expect(screen.getByRole("button", { name: "Continue processing" })).toBeDisabled();
  expect(continueTaskRun).toHaveBeenCalledTimes(1);
});

it("does not expose recovery for an older job", () => {
  state.current.last_failed_job_id = "newer-job";
  render(component());
  expect(screen.queryByRole("button", { name: "Continue processing" })).not.toBeInTheDocument();
});

it("does not carry consent into a changed workflow revision", () => {
  state.current.error = "question_preparation_wall_time_limit_reached";
  const view = render(component());
  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Continue processing" })).toBeEnabled();
  state.current.workflow_revision = 5;
  view.rerender(component());
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Continue processing" })).toBeDisabled();
});


it("keeps the existing stopped-run continue flow without an extra checkbox", async () => {
  render(component());
  expect(screen.getByRole("status")).toHaveTextContent("This run has stopped");
  expect(screen.getByRole("status")).toHaveTextContent("Unfinished model requests may be billed again");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Continue processing" });
  expect(button).toBeEnabled();
  expect(continueTaskRun).not.toHaveBeenCalled();
  fireEvent.click(button);
  await waitFor(() => expect(continueTaskRun).toHaveBeenCalledExactlyOnceWith("task", "job-1", 4, false, true));
});

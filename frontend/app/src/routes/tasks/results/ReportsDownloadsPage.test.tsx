import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { TaskFinalizationResponse } from "@/types";
import { ReportsDownloadsPage } from "./ReportsDownloadsPage";

vi.mock("@/api/tasks", () => ({ getTaskResultArtifacts: vi.fn(), confirmTaskFinalization: vi.fn(), generateTaskResultArtifacts: vi.fn() }));
const api = await import("@/api/tasks");
const finalization = { task_id: "T1", task_status: "graded", workflow_revision: 7, final_result_version: 0,
  final_result_dirty: false, ready_for_confirmation: true, analysis_status: "not_generated" } as TaskFinalizationResponse;

function show(state = finalization) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><ReportsDownloadsPage locale="en-US" taskId="T1" taskName="Example" finalization={state} /></MemoryRouter>
  </QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  const artifacts = { task_id: "T1", current_result_version: 0, analysis_status: "not_generated" as const, versions: [] };
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue(artifacts);
  vi.mocked(api.confirmTaskFinalization).mockResolvedValue({ ...finalization, workflow_revision: 8, final_result_version: 1 });
  vi.mocked(api.generateTaskResultArtifacts).mockResolvedValue({ ...finalization, status: "ok", unchanged: false, artifacts });
});

it("only saves a report snapshot on explicit generation and chains the returned revision", async () => {
  show();
  const button = await screen.findByRole("button", { name: "Generate files" });
  expect(api.confirmTaskFinalization).not.toHaveBeenCalled();
  expect(api.generateTaskResultArtifacts).not.toHaveBeenCalled();
  await userEvent.click(button);
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 8));
  expect(api.confirmTaskFinalization).toHaveBeenCalledWith("T1", 7);
  expect(vi.mocked(api.confirmTaskFinalization).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.generateTaskResultArtifacts).mock.invocationCallOrder[0]);
});

it("reuses an existing current snapshot without confirming it again", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "not_generated",
    versions: [{ version: 1, current: true, status: "not_generated", files: [] }] });
  show({ ...finalization, final_result_version: 1 });
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 7));
  expect(api.confirmTaskFinalization).not.toHaveBeenCalled();
});

it("stops file generation if snapshot preparation conflicts", async () => {
  vi.mocked(api.confirmTaskFinalization).mockRejectedValue(new Error("revision_conflict"));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await screen.findByRole("button", { name: "Generate again" });
  expect(api.generateTaskResultArtifacts).not.toHaveBeenCalled();
});

it("allows explicit regeneration after grades change, without treating it as pending teacher review", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "stale",
    versions: [{ version: 1, current: true, status: "stale", files: [] }] });
  show({ ...finalization, final_result_version: 1, final_result_dirty: true });
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 8));
  expect(screen.queryByText("Return to review")).not.toBeInTheDocument();
});

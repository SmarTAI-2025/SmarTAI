import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ManualQuestionSourceReview } from "./ManualQuestionSourceReview";

const save = vi.fn();
vi.mock("@/api/hooks/tasks", () => ({ useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: save }) }));
vi.mock("@/api/workflowInputs", () => ({ useWorkflowInput: () => ({ data: { input: { job_id: "job", sources: [
  { source_token: "source-1", filename: "first.pdf" }, { source_token: "source-2", filename: "second.pdf" },
] } } }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: ({ blockedFromStep }: { blockedFromStep: number }) => <div>Blocked from step {blockedFromStep}</div> }));
beforeEach(() => save.mockReset().mockResolvedValue({ status: "ready_for_review", first_question_id: "Q1" }));

it("keeps every stopped source in review and requires teacher content plus every source confirmation", async () => {
  render(<I18nProvider><MemoryRouter initialEntries={["/manual"]}><Routes>
    <Route path="/manual" element={<ManualQuestionSourceReview taskId="task" jobId="job" revision={3} />} />
    <Route path="/tasks/task/questions" element={<div>Saved question review</div>} />
  </Routes></MemoryRouter></I18nProvider>);
  expect(screen.getByText("Blocked from step 3")).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "对照原资料" })).toHaveTextContent("first.pdf");
  expect(screen.getAllByRole("checkbox")).toHaveLength(2);
  const submit = screen.getByRole("button", { name: "保存题目并继续复核" });
  expect(submit).toBeDisabled();
  expect(save).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("题号"), { target: { value: "Q1" } });
  fireEvent.change(screen.getByLabelText("题干"), { target: { value: "Teacher question" } });
  fireEvent.change(screen.getByLabelText("标准答案"), { target: { value: "Teacher answer" } });
  fireEvent.change(screen.getByLabelText("评分标准（写明分值）"), { target: { value: "Award 10 points." } });
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  expect(submit).toBeDisabled();
  fireEvent.click(screen.getAllByRole("checkbox")[1]);
  await waitFor(() => expect(submit).toBeEnabled());
  fireEvent.click(submit);
  expect(await screen.findByText("Saved question review")).toBeInTheDocument();
  expect(save).toHaveBeenCalledWith({ taskId: "task", jobId: "job", expectedWorkflowRevision: 3,
    completedSourceTokens: ["source-1", "source-2"], manualQuestions: [{ q_id: "Q1", type: "short", stem: "Teacher question",
      reference_answer: "Teacher answer", criterion: "Award 10 points.", max_score: 10 }] });
});

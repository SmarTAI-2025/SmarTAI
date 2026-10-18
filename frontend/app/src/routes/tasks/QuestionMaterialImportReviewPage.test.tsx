import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { QuestionMaterialImportReviewPage } from "./QuestionMaterialImportReviewPage";

const state = vi.hoisted(() => ({ candidates: [] as unknown[] }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US" }) }));
vi.mock("@/hooks/useDraftProtection", () => ({ useDraftProtection: () => ({ conflict: false }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));
vi.mock("@/components/tasks/MaterialOriginalPreview", () => ({ MaterialOriginalPreview: () => null }));
vi.mock("@/api/hooks", () => ({
  useTask: () => ({ isSuccess: true, data: { problem_data: {} } }),
  useApplyMaterialImport: () => ({ isPending: false }),
  useMaterialImport: () => ({ data: { status: "ready", source: { filename: "answers.txt" }, candidates: state.candidates } }),
}));

it("keeps exact advisory candidates selectable, uses .60 for defaults and protects mismatches and invalid recognition", async () => {
  state.candidates = [
    ["required", 0.2499], ["advisory", 0.25], ["below", 0.5999], ["clear", 0.60],
    ["unknown", null], ["mismatch", 0.99, "possible"], ["invalid", 0.99, "exact", true],
  ].map(([id, confidence, match = "exact", review = false]) => ({
    candidate_id: id, q_id: id, target: "test_cases", confidence, match_status: match,
    recognition_requires_review: review, would_overwrite: false, source_location: "", source_excerpt: "",
    test_cases: [{ input: "1", expected_output: "2" }],
  }));
  render(<MemoryRouter initialEntries={["/tasks/T/questions/import/J"]}><Routes>
    <Route path="/tasks/:taskId/questions/import/:jobId" element={<QuestionMaterialImportReviewPage />} />
  </Routes></MemoryRouter>);
  const checkbox = (name: string) => within(screen.getByRole("cell", { name }).closest("tr")!).getByRole("checkbox");
  for (const id of ["mismatch", "invalid"]) expect(checkbox(id)).toBeDisabled();
  for (const id of ["required", "advisory", "below", "unknown"]) {
    expect(checkbox(id)).toBeEnabled();
    expect(checkbox(id)).not.toBeChecked();
    await userEvent.click(checkbox(id));
    expect(checkbox(id)).toBeChecked();
  }
  expect(checkbox("clear")).toBeChecked();
  expect(screen.getByRole("cell", { name: "unknown" }).closest("tr")).toHaveTextContent("—");
});

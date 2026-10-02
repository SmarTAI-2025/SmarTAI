import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useConfirmResultReviews } from "./useConfirmResultReviews";
import { updateCorrectionReview } from "@/api/tasks";

vi.mock("@/api/tasks", () => ({ updateCorrectionReview: vi.fn() }));
const update = vi.mocked(updateCorrectionReview);
const entries = [
  { studentId: "S1", qId: "Q1", score: 0, comment: "Keep zero" },
  { studentId: "S2", qId: "Q1", score: 8, comment: "Teacher note" },
  { studentId: "S2", qId: "Q2", score: 9, comment: "" },
];
function setup() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(() => useConfirmResultReviews(), {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
  return { ...hook, invalidate };
}
beforeEach(() => vi.clearAllMocks());
describe("bulk result confirmation", () => {
  it("chains server revisions and preserves zero and teacher comments", async () => {
    update.mockResolvedValueOnce({ workflow_revision: 12 } as never)
      .mockResolvedValueOnce({ workflow_revision: 14 } as never)
      .mockResolvedValueOnce({ workflow_revision: 15 } as never);
    const { result, invalidate } = setup();
    await act(async () => { await result.current.mutateAsync({ taskId: "T1", revision: 10, entries }); });
    expect(update.mock.calls.map((call) => call[3].expected_workflow_revision)).toEqual([10, 12, 14]);
    expect(update.mock.calls[0][3]).toMatchObject({ teacher_score: 0, teacher_comment: "Keep zero", confirm: true });
    expect(update.mock.calls[1][3].teacher_comment).toBe("Teacher note");
    expect(result.current.progress).toEqual({ completed: 3, total: 3 });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["tasks", "result", "T1"] });
  });
  it("stops on a stale write, reports completed work, and refreshes without replay", async () => {
    update.mockResolvedValueOnce({ workflow_revision: 11 } as never).mockRejectedValueOnce(new Error("stale"));
    const { result, invalidate } = setup();
    await act(async () => { await expect(result.current.mutateAsync({ taskId: "T1", revision: 10, entries })).rejects.toThrow("stale"); });
    expect(update).toHaveBeenCalledTimes(2);
    expect(result.current.progress).toEqual({ completed: 1, total: 3 });
    expect(invalidate).toHaveBeenCalledTimes(6);
  });
});

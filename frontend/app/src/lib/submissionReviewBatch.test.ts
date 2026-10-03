import { describe, expect, it, vi } from "vitest";
import { confirmSubmissionBatch } from "./submissionReviewBatch";

describe("sequential submission review", () => {
  it("uses the revision returned by the preceding confirmation", async () => {
    const confirm = vi.fn().mockResolvedValueOnce({ workflow_revision: 12 }).mockResolvedValueOnce({ workflow_revision: 15 });
    const result = await confirmSubmissionBatch(["first", "second"], 10, confirm);
    expect(confirm.mock.calls).toEqual([["first", 10], ["second", 12]]);
    expect(result).toMatchObject({ completed: 2, remaining: 0 });
  });

  it("stops on a failed item, preserving confirmed progress without retrying", async () => {
    const stale = new Error("stale_revision");
    const confirm = vi.fn().mockResolvedValueOnce({ workflow_revision: 5 }).mockRejectedValueOnce(stale);
    const result = await confirmSubmissionBatch(["first", "second", "third"], 4, confirm);
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(result).toEqual({ completed: 1, remaining: 2, failedItem: "second", error: stale });
  });
});

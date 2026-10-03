import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { useGradingIntent } from "./useGradingIntent";

describe("explicit grading intents", () => {
  it("replays an uncertain network attempt with the same key and revision, then uses a new intent", async () => {
    const { result } = renderHook(useGradingIntent);
    const submit = vi.fn().mockRejectedValueOnce(new APIError(0, "Network timeout")).mockResolvedValue("done");
    await act(async () => { await expect(result.current.execute(4, submit)).rejects.toThrow("Network timeout"); });
    await act(async () => { await result.current.execute(5, submit); });
    expect(submit.mock.calls[1][0]).toEqual(submit.mock.calls[0][0]);
    expect(submit.mock.calls[1][0].expectedWorkflowRevision).toBe(4);
    await act(async () => { await result.current.execute(5, submit); });
    expect(submit.mock.calls[2][0].requestId).not.toBe(submit.mock.calls[0][0].requestId);
    expect(submit.mock.calls[2][0].expectedWorkflowRevision).toBe(5);
  });

  it("blocks a synchronous double click before mutation state rerenders", async () => {
    const { result } = renderHook(useGradingIntent);
    let resolve!: (value: string) => void;
    const submit = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
    await act(async () => {
      const first = result.current.execute(4, submit);
      expect(await result.current.execute(4, submit)).toBeUndefined();
      resolve("done");
      await first;
    });
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("allows a repaired definite rejection to use the new workflow revision", async () => {
    const { result } = renderHook(useGradingIntent);
    const submit = vi.fn().mockRejectedValueOnce(new APIError(409, "workflow_revision_conflict")).mockResolvedValue("done");
    await act(async () => { await expect(result.current.execute(4, submit)).rejects.toThrow(); });
    await act(async () => { await result.current.execute(6, submit); });
    expect(submit.mock.calls[1][0].requestId).not.toBe(submit.mock.calls[0][0].requestId);
    expect(submit.mock.calls[1][0].expectedWorkflowRevision).toBe(6);
  });
});

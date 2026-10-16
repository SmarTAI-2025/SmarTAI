import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getGradingActivity } from "@/api/tasks";
import { GradingActivity, ModelProgressRows } from "./GradingActivity";
import type { GradingActivityEvent, GradingModelProgress, JobProgress } from "@/types/progress";

vi.mock("@/api/tasks", () => ({ getGradingActivity: vi.fn() }));
const getActivity = vi.mocked(getGradingActivity);
const model = (label: string, id: string, succeeded: number): GradingModelProgress => ({
  label, id, total: 14, succeeded, failed: 0, cancelled: 0, processing: 0, queued: 0,
  responding: 14 - succeeded, retrying: 0, completion_times: [],
});
const event = (sequence: number, code: GradingActivityEvent["code"] = "succeeded"): GradingActivityEvent => ({
  sequence, code, ts: 1_790_000_000 + sequence, model_id: "model-1",
});
const progress = (latest = event(3)): JobProgress => ({
  phase: "grading", job_id: "run-1", total_students: 2, total_questions: 7, completed_units: 1,
  messages: [], active: [], model_progress: [model("Gemini", "model-1", 14), model("清华模型", "model-2", 1)],
  activity_cursor: latest.sequence, activity_latest: latest,
});

describe("factual grading activity", () => {
  beforeEach(() => vi.resetAllMocks());

  it("keeps model rows visible while only the smaller recent activity is collapsed", async () => {
    const snapshot = progress();
    getActivity.mockResolvedValue({ run_id: "run-1", events: [event(1), event(2), event(3)], cursor: 3, has_more: false });
    const view = render(<><ModelProgressRows models={snapshot.model_progress!} locale="zh-CN" />
      <GradingActivity taskId="task" progress={snapshot} locale="zh-CN" /></>);
    expect(screen.getByText(/已处理 14 \/ 14/)).toBeVisible();
    expect(screen.getByText(/已处理 1 \/ 14/)).toBeVisible();
    expect(getActivity).not.toHaveBeenCalled();
    const toggle = screen.getByRole("button", { name: /最近动态/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    view.rerender(<><ModelProgressRows models={snapshot.model_progress!} locale="zh-CN" />
      <GradingActivity taskId="task" progress={progress(event(3, "responding"))} locale="zh-CN" /></>);
    expect(toggle).toHaveTextContent("请求已发出，等待模型响应");
    fireEvent.click(toggle);
    const region = await screen.findByRole("region", { name: "批改动态历史" });
    expect(await within(region).findAllByRole("listitem")).toHaveLength(3);
    expect(region).toHaveClass("h-56", "overflow-y-auto");
    fireEvent.click(toggle);
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    expect(screen.getByText(/已处理 14 \/ 14/)).toBeVisible();
  });

  it("reads every history page across internal-event gaps without duplicates", async () => {
    getActivity.mockResolvedValueOnce({ run_id: "run-1", events: [event(1)], cursor: 200, has_more: true })
      .mockResolvedValueOnce({ run_id: "run-1", events: [event(201), event(205)], cursor: 205, has_more: false });
    render(<GradingActivity taskId="task" progress={progress(event(205))} locale="en-US" />);
    fireEvent.click(screen.getByRole("button", { name: /Recent activity/ }));
    await screen.findAllByRole("listitem");
    expect(await screen.findAllByRole("listitem")).toHaveLength(3);
    expect(getActivity.mock.calls.map((call) => call[2])).toEqual([0, 200]);
  });

  it("appends real new events while expanded and preserves old history", async () => {
    vi.useFakeTimers();
    try {
      getActivity.mockResolvedValueOnce({ run_id: "run-1", events: [event(3)], cursor: 3, has_more: false })
        .mockResolvedValueOnce({ run_id: "run-1", events: [event(4, "retrying")], cursor: 4, has_more: false });
      const view = render(<GradingActivity taskId="task" progress={progress()} locale="en-US" />);
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Recent activity/ })); });
      view.rerender(<GradingActivity taskId="task" progress={progress(event(4, "retrying"))} locale="en-US" />);
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
      expect(screen.getAllByRole("listitem")).toHaveLength(2);
      expect(getActivity.mock.calls.map((call) => call[2])).toEqual([0, 3]);
    } finally { vi.useRealTimers(); }
  });

  it("retries a failed history read and clears history on a new run", async () => {
    getActivity.mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({ run_id: "run-1", events: [event(3)], cursor: 3, has_more: false });
    const view = render(<GradingActivity key="run-1" taskId="task" progress={progress()} locale="en-US" />);
    fireEvent.click(screen.getByRole("button", { name: /Recent activity/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Activity could not load/ }));
    expect(await screen.findAllByRole("listitem")).toHaveLength(1);
    view.rerender(<GradingActivity key="run-2" taskId="task" progress={{ ...progress(), job_id: "run-2", activity_latest: null }} locale="en-US" />);
    expect(screen.getByRole("button", { name: /Recent activity/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(getActivity.mock.calls[0][3]?.aborted).toBe(true);
  });

  it("shows a stopped state instead of stale response waits", () => {
    render(<><ModelProgressRows models={progress().model_progress!} locale="en-US" stopped />
      <GradingActivity taskId="task" progress={progress(event(3, "responding"))} locale="en-US" stopped /></>);
    expect(screen.getByText(/13 unfinished/)).toBeVisible();
    expect(screen.queryByText(/13 awaiting response/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recent activity/ })).toHaveTextContent("This run has stopped");
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getJSON, postJSON } from "./client";
import { getPerQuestionBreakdown, interpretFilterIntent, runAnalyticsQuery } from "./analytics";
import { interpretTaskHistoryQuery } from "./tasks";

vi.mock("./client", () => ({ postJSON: vi.fn(), getJSON: vi.fn(), deleteJSON: vi.fn(), putJSON: vi.fn() }));

describe("model request deadlines", () => {
  beforeEach(() => vi.clearAllMocks());

  it.each(["chart", "summary", "filter"] as const)("allows a five-minute wait for %s generation", async (mode) => {
    vi.mocked(postJSON).mockResolvedValue({ mode });
    await runAnalyticsQuery("task-demo", "analyze this", mode);
    expect(postJSON).toHaveBeenCalledExactlyOnceWith("/analytics/task-demo/query", { question: "analyze this", mode }, { timeout: 300_000 });
  });

  it("uses the same five-minute allowance for question, student and review interpretation", async () => {
    for (const surface of ["question_analysis", "student_analysis", "review_overview"] as const) {
      await interpretFilterIntent("task-demo", "lowest first", surface);
      expect(postJSON).toHaveBeenLastCalledWith("/analytics/task-demo/filter-intent", { question: "lowest first", surface }, { timeout: 300_000 });
    }
    expect(postJSON).toHaveBeenCalledTimes(3);
  });

  it("also covers the separate Current task history Ask endpoint", async () => {
    await interpretTaskHistoryQuery("unfinished tasks");
    expect(postJSON).toHaveBeenCalledExactlyOnceWith("/tasks/query/interpret", { query: "unfinished tasks" }, { timeout: 300_000 });
  });

  it("allows five minutes for the first model-backed per-question mistake analysis", async () => {
    await getPerQuestionBreakdown("task-demo", "Q1");
    expect(getJSON).toHaveBeenCalledExactlyOnceWith("/analytics/task-demo/per_question/Q1", { timeout: 300_000 });
  });
});

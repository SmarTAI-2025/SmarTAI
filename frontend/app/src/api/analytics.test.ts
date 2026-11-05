import { beforeEach, describe, expect, it, vi } from "vitest";
import { getJSON, postJSON } from "./client";
import { getPerQuestionBreakdown, interpretFilterIntent, runAnalyticsQuery } from "./analytics";
import { interpretTaskHistoryQuery } from "./tasks";

vi.mock("./client", () => ({ postJSON: vi.fn(), getJSON: vi.fn(), deleteJSON: vi.fn(), putJSON: vi.fn() }));

describe("model request deadlines", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(postJSON).mockResolvedValue({recognized:true,kind:"table",explanation:"queried",data:{columns:[],rows:[]},selection:null,chart:null});
  });

  it.each(["chart", "summary", "filter"] as const)("allows processing plus cleanup for %s generation", async (mode) => {
    vi.mocked(postJSON).mockResolvedValue({ mode });
    await runAnalyticsQuery("task-demo", "analyze this", mode);
    expect(postJSON).toHaveBeenCalledExactlyOnceWith("/analytics/task-demo/query", { question: "analyze this", mode }, { timeout: 305_000 });
  });

  it("uses the same processing and cleanup allowance for question, student and review interpretation", async () => {
    for (const surface of ["question_analysis", "student_analysis", "review_overview"] as const) {
      await interpretFilterIntent("task-demo", "lowest first", surface);
      expect(postJSON).toHaveBeenLastCalledWith("/analytics/task-demo/ask", { question: "lowest first", surface }, { timeout: 305_000 });
    }
    expect(postJSON).toHaveBeenCalledTimes(3);
  });

  it("also covers the separate Current task history Ask endpoint", async () => {
    await interpretTaskHistoryQuery("unfinished tasks");
    expect(postJSON).toHaveBeenCalledExactlyOnceWith("/analytics/ask", { question: "unfinished tasks", surface: "history" }, { timeout: 305_000 });
  });

  it("lets per-question analysis finish cleanup after its25s processing cap", async () => {
    await getPerQuestionBreakdown("task-demo", "Q1");
    expect(getJSON).toHaveBeenCalledExactlyOnceWith("/analytics/task-demo/per_question/Q1", { timeout: 40_000 });
  });
});

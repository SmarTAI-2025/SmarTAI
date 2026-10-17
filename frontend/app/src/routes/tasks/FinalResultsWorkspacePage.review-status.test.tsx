import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { Correction } from "@/types";
import { FinalResultsWorkspacePage } from "./FinalResultsWorkspacePage";

const state = vi.hoisted(() => ({ locale: "zh-CN", correction: {} as Correction }));
vi.mock("@/api/hooks/tasks", () => ({
  useTask: () => ({ data: { task_id: "task", name: "Confirmation test", status: "finalized" } }),
  useTaskFinalization: () => ({ data: { final_result_version: 1, final_result_dirty: false, analysis_status: "not_generated" } }),
  useTaskResult: () => ({ data: {
    status: "completed", task_id: "task",
    problem_data: { q1: { q_id: "q1", number: "1", stem: "Synthetic question", criterion: "Synthetic rubric", max_score: 10 } },
    results: [{ student_id: "S1", student_name: "Example", corrections: [state.correction] }],
  } }),
}));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: state.locale, t: (key: string) => key }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));

beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
  state.correction = {
    q_id: "q1", type: "proof", score: 6, provisional_score: 6, max_score: 10,
    confidence: .4, comment: "Original explanation", steps: [], expert_results: [],
    requires_human_review: true, review_reasons: ["low_confidence", "general"], review_status: "pending",
  };
});

for (const locale of ["zh-CN", "en-US"]) {
  it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"].flatMap(suffix => [0.2, 0.95].map(confidence => ({ suffix, confidence }))))(
    `${locale} shows only required reviews on results$suffix at confidence $confidence`, ({ suffix, confidence }) => {
      state.locale = locale;
      state.correction = { ...state.correction, confidence, review_reasons: [], requires_human_review: false,
        expert_results: [0, 8].map(score => ({ provider: `expert-${score}`, score, max_score: 10, confidence: .95, comment: "", steps: [] })) };
      const path = `/tasks/:taskId/results${suffix.replace("q1", ":questionId").replace("S1", ":studentId")}`;
      const { container } = render(<QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[`/tasks/task/results${suffix}`]}><Routes><Route path={path} element={<FinalResultsWorkspacePage />} /></Routes></MemoryRouter>
      </QueryClientProvider>);
      if (confidence < 0.25) expect(container.textContent).toMatch(/待确认|待复核|待处理|awaiting|Review pending|1 pending/);
      else expect(container.textContent).not.toMatch(/[1-9]\d* 项待|[1-9]\d* awaiting|Review pending|1 pending/);
    },
  );
  for (const confirmation of ["unchanged", "changed"] as const) {
    it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"])(
      `${locale} ${confirmation} confirmation removes stale score/review claims on results%s`, (suffix) => {
        state.locale = locale;
        state.correction = { ...state.correction, teacher_score: confirmation === "unchanged" ? 6 : 8, review_status: "confirmed" };
        const path = `/tasks/:taskId/results${suffix.replace("q1", ":questionId").replace("S1", ":studentId")}`;
        const { container } = render(<QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={[`/tasks/task/results${suffix}`]}>
            <Routes><Route path={path} element={<FinalResultsWorkspacePage />} /></Routes>
          </MemoryRouter>
        </QueryClientProvider>);
        expect(screen.getAllByText(/教师已|teacher.handled|teacher confirmed|teacher changed/i).length).toBeGreaterThan(0);
        expect(container.textContent).not.toMatch(/AI\s*(?:分|score)|未人工|教师未操作|Unreviewed|not reviewed|no teacher action/);
        expect(container.textContent).not.toMatch(/系统标记需人工复核|System requested human review|需要人工确认|Teacher review required/);
        expect(container.textContent).toContain(confirmation === "unchanged" ? "60%" : "80%");
      },
    );
  }
  it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"])(
    `${locale} failed experts and routine confidence do not invent pending items on results%s`, (suffix) => {
      state.locale = locale;
      state.correction = { ...state.correction, confidence: .5, review_reasons: [], requires_human_review: false,
        expert_results: [{ provider: "valid", score: 6, max_score: 10, confidence: .5, comment: "", steps: [] },
          { provider: "failed", score: 0, error_kind: "general", max_score: 10, confidence: 0, comment: "", steps: [] },
          { provider: "missing", score: null as unknown as number, max_score: 10, confidence: 0, comment: "", steps: [] }] };
      const path = `/tasks/:taskId/results${suffix.replace("q1", ":questionId").replace("S1", ":studentId")}`;
      const { container } = render(<QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[`/tasks/task/results${suffix}`]}>
          <Routes><Route path={path} element={<FinalResultsWorkspacePage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>);
      expect(container.textContent).not.toMatch(/AI\s*(?:分|score)|未人工|教师未操作|Unreviewed|not reviewed|no teacher action|[1-9]\d* 项待|[1-9]\d* awaiting/);
      expect(container.textContent).not.toMatch(/[1-9]\d* 个专家分歧|[1-9]\d* expert-disagreement/);
      expect(container.textContent).not.toMatch(/(?:[1-9]\d* 个低置信|[1-9]\d* low-confidence|Low-confidence items)/);
      expect(container.textContent).toContain("60%");
    },
  );
}

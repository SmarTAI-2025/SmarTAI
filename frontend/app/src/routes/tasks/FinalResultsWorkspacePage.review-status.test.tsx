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
    q_id: "q1", type: "reasoning", score: 6, provisional_score: 6, max_score: 10,
    confidence: .4, comment: "Original explanation", steps: [], expert_results: [],
    requires_human_review: true, review_reasons: ["low_confidence", "general"], review_status: "pending",
  };
});

for (const locale of ["zh-CN", "en-US"]) {
  it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"].flatMap(suffix => [0.2, 0.95].map(confidence => ({ suffix, confidence }))))(
    `${locale} shows disagreement review signals on results$suffix at confidence $confidence`, ({ suffix, confidence }) => {
      state.locale = locale;
      state.correction = { ...state.correction, confidence, review_reasons: [], requires_human_review: false,
        expert_results: [0, 8].map(score => ({ provider: `expert-${score}`, score, max_score: 10, confidence: .95, comment: "", steps: [] })) };
      const path = `/tasks/:taskId/results${suffix.replace("q1", ":questionId").replace("S1", ":studentId")}`;
      const { container } = render(<QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[`/tasks/task/results${suffix}`]}><Routes><Route path={path} element={<FinalResultsWorkspacePage />} /></Routes></MemoryRouter>
      </QueryClientProvider>);
      expect(container.textContent).not.toMatch(/标准差|standard deviation|15%/);
      expect(container.textContent).toMatch(/待确认|待复核|待处理|awaiting|Review pending|1 pending/);
      expect(screen.getByRole("link", { name: locale === "zh-CN" ? /专家评分差异较大.*不影响查看/ : /large differences in expert scores.*optional/ })).toHaveAttribute("href", "/tasks/task/review");
    },
  );
  for (const confirmation of ["unchanged", "changed"] as const) {
    it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"])(
      `${locale} ${confirmation} confirmation removes stale score/review claims on results%s`, (suffix) => {
        state.locale = locale;
        state.correction = { ...state.correction, teacher_score: confirmation === "unchanged" ? 6 : 8, review_status: "confirmed",
          expert_results: [10, 6].map(score => ({ provider: "expert", score, max_score: 10, confidence: .95, comment: "", steps: [] })) };
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
        expect(screen.queryByRole("link", { name: /专家评分差异较大.*不影响查看|large differences in expert scores.*optional/ })).not.toBeInTheDocument();
      },
    );
  }
  it.each(["", "/questions", "/questions/q1", "/students", "/students/S1"])(
    `${locale} partial experts and low confidence retain usable results and advisory signals on results%s`, (suffix) => {
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
      expect(container.textContent).not.toMatch(/AI\s*(?:分|score)|未人工|教师未操作|Unreviewed|not reviewed|no teacher action/);
      expect(container.textContent).not.toMatch(/[1-9]\d* 个专家分歧|[1-9]\d* expert-disagreement/);
      expect(screen.queryByRole("link", { name: /专家评分差异较大|large differences in expert scores/ })).not.toBeInTheDocument();
      expect(container.textContent).not.toMatch(/缺少有效分数|Missing valid score/);
      expect(container.textContent).toContain("60%");
    },
  );
}

it.each(["zh-CN", "en-US"])("keeps the score-variance banner count separate from item-level disagreement (%s)", locale => {
  state.locale = locale;
  state.correction = { ...state.correction, confidence: .95, review_reasons: ["math_expert_disagreement"],
    expert_results: [1, 2].map(index => ({ provider: `expert-${index}`, score: 8, max_score: 10, confidence: .95, comment: "", steps: [] })) };
  render(<QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/tasks/task/results"]}><Routes><Route path="/tasks/:taskId/results" element={<FinalResultsWorkspacePage />} /></Routes></MemoryRouter>
  </QueryClientProvider>);
  expect(screen.queryByRole("link", { name: /专家评分差异较大|large differences in expert scores/ })).not.toBeInTheDocument();
});

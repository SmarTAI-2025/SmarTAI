import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";

const extraIssues = vi.hoisted(() => [] as Array<Record<string, string>>);
const mutateAsync = vi.hoisted(() => vi.fn());
beforeEach(() => { extraIssues.length = 0; mutateAsync.mockReset().mockResolvedValue({ workflow_revision: 8 }); });

vi.mock("@/api/hooks/tasks", () => ({
  useUpdateProblem: () => ({ isPending: false, mutateAsync }),
  useTask: () => ({
    isLoading: false,
    isSuccess: true,
    data: {
      task_id: "task-1",
      status: "problems_ready",
      workflow_revision: 7,
      problem_data: {
        Q1: {
          q_id: "Q1",
          number: "Q1",
          type: "概念题",
          stem: "三角函数基础",
          max_score: 10,
          max_score_source: "default_10",
          max_score_review_status: "needs_review",
          criterion: "说明基本概念",
          question_structure: {
            contract_version: 1,
            scoring_unit: "major_question",
            major_number: "Q1",
            major_order: 0,
            shared_stem: "三角函数基础",
            subparts: [
              { subpart_id: "sp1", label: "(a)", order: 0, stem: "定义", source_span_ids: [] },
              { subpart_id: "sp2", label: "(b)", order: 1, stem: "证明", source_span_ids: [] },
            ],
            structure_source: "deterministic",
            review_status: "confirmed",
          },
          preparation_issues: [{
            issue_id: "score-risk-1",
            field: "max_score",
            code: "default_max_score_requires_review",
            severity: "warning",
            status: "open",
          }, ...extraIssues],
        },
        Q2: {
          q_id: "Q2",
          number: "Q2",
          type: "计算题",
          stem: "计算积分",
          max_score: 6,
          max_score_source: "per_question_text",
          max_score_review_status: "confirmed",
          criterion: "步骤正确",
          preparation_issues: [],
        },
      },
    },
  }),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => null,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN" }),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

function renderPage(initialEntry: string) {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/tasks/:taskId/questions"
          element={(
            <>
              <QuestionPreparationOverviewPage />
              <LocationProbe />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>,
  );
  return screen.getByRole("textbox", { name: "Ask SmarTAI：题目资料" }) as HTMLInputElement;
}

describe("QuestionPreparationOverviewPage smart search", () => {
  it("offers response upload even when unreviewed questions are hidden by a no-match filter", () => {
    renderPage("/tasks/task-1/questions?q=no-match");
    expect(screen.getByRole("link", { name: "继续上传作答" })).toHaveAttribute("href", "/tasks/task-1/submissions/upload");
    expect(mutateAsync).not.toHaveBeenCalled();
  });
  it("keeps only a details link in each matrix action cell", () => {
    renderPage("/tasks/task-1/questions");
    expect(screen.queryByRole("button", { name: /确认第/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "查看" })).toHaveLength(2);
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("labels and confirms only the current filtered questions", async () => {
    renderPage("/tasks/task-1/questions?q=Q2");
    await userEvent.click(await screen.findByRole("button", { name: "确认筛选项（1）" }));
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ taskId: "task-1", qId: "Q2", expectedWorkflowRevision: 7, review_status: "confirmed" });
  });

  it("chains returned revisions across all visible questions", async () => {
    renderPage("/tasks/task-1/questions");
    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));
    expect(mutateAsync.mock.calls.map(([patch]) => [patch.qId, patch.expectedWorkflowRevision])).toEqual([["Q1", 7], ["Q2", 8]]);
  });

  it.each([
    ["recognition_partial", "部分转写内容可信度较低，请对照原文件检查公式、符号和条件。"],
    ["recognition_needs_review", "部分转写内容可信度较低，请对照原文件检查公式、符号和条件。"],
    ["future_backend_issue", "资料存在待核对项，请打开详情"],
  ])("renders the review matrix for %s without crashing", (code, label) => {
    extraIssues.push({ issue_id: "ocr-risk", field: "stem", code, severity: "warning", status: "open" });
    renderPage("/tasks/task-1/questions");
    expect(screen.getAllByTitle(label).length).toBeGreaterThan(0);
    expect(screen.getByRole("row", { name: /Q1/ })).toBeInTheDocument();
  });

  it("blocks the entire batch and links the failed question before writing", async () => {
    extraIssues.push({ issue_id: "failed", field: "stem", code: "parse_anomaly", severity: "blocking", status: "open" });
    renderPage("/tasks/task-1/questions");
    await userEvent.click(screen.getByRole("button", { name: "全部确认" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByRole("button", {name: "前往问题位置"})).toBeEnabled();
  });

  it("shows each maximum score and the total while flagging defaults", () => {
    renderPage("/tasks/task-1/questions");

    expect(screen.getByRole("columnheader", { name: "满分" })).toBeInTheDocument();
    expect(screen.getByTitle("系统默认，需确认")).toHaveTextContent("10 分");
    expect(screen.getByText(/作业总分 16/)).toBeInTheDocument();
    expect(screen.getByTitle("系统默认，需确认")).toHaveClass("bg-emerald-100");
    expect(screen.getByRole("region", { name: "待复核队列" })).toHaveTextContent("当前没有需要复核的题目");
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(screen.queryByRole("row", { name: /\(a\)/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /\(b\)/ })).not.toBeInTheDocument();
  });

  it("does not apply a native composing input event before composition ends", async () => {
    const input = renderPage("/tasks/task-1/questions?status=open");

    fireEvent.input(input, {
      target: { value: "san" },
      isComposing: true,
      inputType: "insertCompositionText",
    });

    expect(input).toHaveValue("san");
    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=open");
    expect(screen.getByRole("row", { name: /Q1/ })).toBeInTheDocument();

    fireEvent.compositionEnd(input, { data: "三" });
    fireEvent.change(input, { target: { value: "三" } });

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("status=open&q=%E4%B8%89");
    });
  });

  it("keeps the caret before the Chinese character across repeated deletions", async () => {
    const user = userEvent.setup();
    const input = renderPage("/tasks/task-1/questions?q=ssasan%E4%B8%89");
    input.focus();
    input.setSelectionRange(6, 6);

    const edits = [
      ["ssasa三", 5],
      ["ssas三", 4],
      ["ssa三", 3],
      ["ss三", 2],
      ["s三", 1],
      ["三", 0],
    ] as const;

    for (const [value, caret] of edits) {
      await user.keyboard("{Backspace}");
      await waitFor(() => {
        expect(screen.getByTestId("location-search")).toHaveTextContent(`q=${encodeURIComponent(value)}`);
      });
      expect(input).toHaveValue(value);
      expect(input.selectionStart).toBe(caret);
      expect(input.selectionEnd).toBe(caret);
      expect(input).toHaveFocus();
    }
  });
});

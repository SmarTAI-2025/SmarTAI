import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QuestionPreparationOverviewPage } from "./QuestionPreparationOverviewPage";

const extraIssues = vi.hoisted(() => [] as Array<Record<string, string>>);
const mutateAsync = vi.hoisted(() => vi.fn());
const manualTask = vi.hoisted(() => ({} as Record<string, unknown>));
const createRows = vi.hoisted(() => vi.fn());
const addRow = vi.hoisted(() => vi.fn());
const deleteRow = vi.hoisted(() => vi.fn());
beforeEach(() => { addRow.mockReset().mockResolvedValue({ workflow_revision: 8 }); deleteRow.mockReset().mockResolvedValue({ workflow_revision: 8 }); });
beforeEach(() => { Object.keys(manualTask).forEach(key => delete manualTask[key]); createRows.mockReset().mockResolvedValue({ workflow_revision: 8 }); extraIssues.length = 0; mutateAsync.mockReset().mockResolvedValue({ workflow_revision: 8 }); });

vi.mock("@/api/hooks/tasks", () => ({
  useAddTaskQuestion: () => ({ isPending: false, mutateAsync: addRow }),
  useDeleteTaskQuestion: () => ({ isPending: false, mutateAsync: deleteRow }),
  useManuallyCompleteQuestionPreparation: () => ({ isPending: false, mutateAsync: createRows }),
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
      ...manualTask,
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
        <Route path="/tasks/:taskId/problems/progress" element={<p>Organizing saved text</p>} />
        <Route path="/tasks/:taskId/submissions/upload" element={<p>Upload submissions</p>} />
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
  it("offers response upload even when ordinary unconfirmed questions are hidden by a no-match filter", async () => {
    renderPage("/tasks/task-1/questions?q=no-match");
    await userEvent.click(screen.getByRole("button", { name: "进入上传作答" }));
    expect(screen.getByText("Upload submissions")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
  });
  it("keeps the details link and delete icon in each matrix action cell", () => {
    renderPage("/tasks/task-1/questions");
    expect(screen.queryByRole("button", { name: /确认第/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "查看" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "删除" })).toHaveLength(2);
    expect(screen.getAllByTitle("查看")).toHaveLength(2);
    expect(screen.getAllByTitle("删除")).toHaveLength(2);
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("confirms only suggested questions visible in the current queue", async () => {
    renderPage("/tasks/task-1/questions?q=Q2");
    await userEvent.click(await screen.findByRole("button", { name: "全部确认" }));
    expect(mutateAsync.mock.calls.map(([input]) => input.qId)).toEqual(["Q2"]);
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
    expect(within(screen.getByRole("region", { name: "待复核队列" })).getAllByRole("link")).toHaveLength(2);
    expect(screen.getByRole("row", { name: /Q1/ })).toBeInTheDocument();
  });

  it("offers no bulk confirmation for required items and still blocks progression", async () => {
    extraIssues.push({ issue_id: "failed", field: "stem", code: "parse_anomaly", severity: "blocking", status: "open" });
    renderPage("/tasks/task-1/questions");
    expect(screen.queryByRole("button", { name: "全部确认" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "进入上传作答" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.getByRole("button", {name: "前往问题位置"})).toBeEnabled();
  });

  it("blocks the next stage for a serious question hidden by a no-match filter", async () => {
    extraIssues.push({ issue_id: "failed", field: "stem", code: "parse_anomaly", severity: "blocking", status: "open" });
    renderPage("/tasks/task-1/questions?q=no-match");
    await userEvent.click(screen.getByRole("button", { name: "进入上传作答" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.queryByText("Upload submissions")).not.toBeInTheDocument();
  });

  it("shows each maximum score and the total while flagging defaults", () => {
    renderPage("/tasks/task-1/questions");

    expect(screen.getByRole("columnheader", { name: "满分" })).toBeInTheDocument();
    expect(screen.getByTitle("系统默认，需确认")).toHaveTextContent("10 分");
    expect(screen.getByText(/作业总分 16/)).toBeInTheDocument();
    expect(screen.getByTitle("系统默认，需确认")).toHaveClass("bg-amber-100");
    expect(screen.getByRole("region", { name: "待复核队列" })).toHaveTextContent("建议处理 (2)");
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

it("adds beside the statistics with the same control height and confirms deletion before sending", async () => {
  renderPage("/tasks/task-1/questions");
  const add = screen.getByRole("button", { name: "添加题目" });
  expect(add).toHaveClass("h-10");
  expect(add.closest("[data-review-stage-note]")).toHaveTextContent("作业总分 16");
  expect(screen.getByRole("button", { name: "进入上传作答" })).toHaveClass("h-10");
  await userEvent.click(add);
  expect(addRow).toHaveBeenCalledWith({ taskId: "task-1", expectedWorkflowRevision: 7 });
  await userEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  expect(deleteRow).not.toHaveBeenCalled();
  await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "取消" }));
  expect(deleteRow).not.toHaveBeenCalled();
  await userEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "删除" }));
  expect(deleteRow).toHaveBeenCalledWith({ taskId: "task-1", qId: "Q1", expectedWorkflowRevision: 7 });
});

it("allows adding after the last row was removed and blocks the next stage", async () => {
  Object.assign(manualTask, { problem_data: {} });
  renderPage("/tasks/task-1/questions");
  expect(screen.getByRole("button", { name: "添加题目" })).toBeEnabled();
  await userEvent.click(screen.getByRole("button", { name: "进入上传作答" }));
  expect(screen.getByRole("alertdialog")).toBeVisible();
  expect(screen.queryByText("Upload submissions")).not.toBeInTheDocument();
});

it("does not offer row mutations after grading", () => {
  Object.assign(manualTask, { status: "graded" });
  renderPage("/tasks/task-1/questions");
  expect(screen.queryByRole("button", { name: "添加题目" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
});

it("clears a filter after adding so the new blank row can be seen", async () => {
  renderPage("/tasks/task-1/questions?q=no-match&sort=max_score_desc&column_sort=max_score:desc");
  await userEvent.click(screen.getByRole("button", { name: "添加题目" }));
  await waitFor(() => expect(screen.getByTestId("location-search")).toBeEmptyDOMElement());
});

it("keeps early manual recovery inside the existing matrix and creates only the requested blank rows", async () => {
  Object.assign(manualTask, { problem_data: {}, extract_job_id: "early", manual_question_count_required: true });
  renderPage("/tasks/task-1/questions?manualJob=early");
  expect(screen.getByRole("heading", { name: "题目资料总览" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "人工补齐题目" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "待复核队列" })).toBeVisible();
  fireEvent.change(screen.getByRole("spinbutton", { name: "题数" }), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "建立题目行" }));
  await waitFor(() => expect(createRows).toHaveBeenCalledWith({ taskId: "task-1", jobId: "early", expectedWorkflowRevision: 7, questionCount: 2 }));
});

it("does not offer retired saved-text organization from the original matrix", () => {
  Object.assign(manualTask, { problem_data: {}, extract_job_id: "early", manual_question_count_required: true });
  renderPage("/tasks/task-1/questions");
  expect(screen.queryByRole("button", { name: "整理已保存的识别文字" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "建立题目行" })).toBeVisible();
  expect(createRows).not.toHaveBeenCalled();
});

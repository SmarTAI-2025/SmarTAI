import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { HistoryInterpretation, TaskLite } from "@/types";
import { HistoryPage } from "./HistoryPage";

const mocks = vi.hoisted(() => ({ history: vi.fn(), interpret: vi.fn(), state: vi.fn() }));
vi.mock("@/api/tasks", () => ({ getTaskState: mocks.state }));
vi.mock("@/components/history/HistoryTagPopover", () => ({ HistoryTagPopover: () => null }));
vi.mock("@/api/hooks", () => ({
  useTaskHistory: mocks.history,
  useTags: () => ({ data: [] }),
  useDeleteTask: () => ({ isPending: false }),
  useInterpretTaskHistoryQuery: () => ({ mutateAsync: mocks.interpret, isPending: false, reset: vi.fn() }),
}));

const queryClients: QueryClient[] = [];
function mount(initial = "/history") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClients.push(client);
  render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter initialEntries={[initial]}><HistoryPage /></MemoryRouter></I18nProvider></QueryClientProvider>);
  return client;
}
afterEach(() => { queryClients.splice(0).forEach(client => client.clear()); vi.useRealTimers(); });
function ask() {
  fireEvent.change(screen.getByRole("textbox", { name: "SmarTAI 智能筛选任务" }), { target: { value: "还有什么没有批完" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask SmarTAI" }));
}
const interpretation: HistoryInterpretation = {
  filters: { unfinished: true }, sort: "updated_desc", conditions: [], ambiguities: [],
  explanation: "只显示尚未完成的任务。", source: "llm",
};

describe("Current task Ask SmarTAI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.state.mockReset().mockImplementation(async (taskId: string) => ({ task_id: taskId, status: "grading" }));
    window.localStorage.removeItem("smartai_locale");
    mocks.history.mockReturnValue({ data: { items: [], total: 0 }, isLoading: false, isFetching: false, error: null });
  });

  it("applies the semantic interpretation and shows its explanation", async () => {
    mocks.interpret.mockResolvedValue(interpretation);
    mount();
    ask();
    expect(await screen.findByText(interpretation.explanation)).toBeInTheDocument();
    expect(mocks.interpret).toHaveBeenCalledExactlyOnceWith("还有什么没有批完");
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith(expect.objectContaining({ unfinished: true })));
  });

  it("sorts task history on the server and reverses a second header click", async () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "任务，当前未排序；点击升序" }));
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "name_asc" })));
    fireEvent.click(screen.getByRole("button", { name: "任务，当前升序；点击降序" }));
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "name_desc" })));
    expect(screen.queryByRole("combobox", { name: "排序" })).not.toBeInTheDocument();
  });

  it("does not replace a user's cleared filter when an older model response arrives", async () => {
    let finish!: (value: HistoryInterpretation) => void;
    mocks.interpret.mockReturnValue(new Promise<HistoryInterpretation>((resolve) => { finish = resolve; }));
    mount();
    ask();
    fireEvent.click(screen.getByRole("button", { name: "清空筛选" }));
    await act(async () => finish(interpretation));
    expect(screen.queryByText(interpretation.explanation)).not.toBeInTheDocument();
    expect(mocks.history).toHaveBeenLastCalledWith(expect.not.objectContaining({ unfinished: true }));
  });

  it.each([["进度", "progress"], ["预计剩余", "eta"]])("sorts %s on the server, reverses and resets pagination", async (label, key) => {
    mocks.history.mockReturnValue({ data: { items: [], total: 70 }, isLoading: false, error: null });
    mount("/history?page=2");
    fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${label}，`) }));
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith(expect.objectContaining({ sort: `${key}_asc`, page: 1 })));
    expect(screen.getByRole("columnheader", { name: label })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${label}，`) }));
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith(expect.objectContaining({ sort: `${key}_desc`, page: 1 })));
    expect(screen.getByRole("columnheader", { name: label })).toHaveAttribute("aria-sort", "descending");
  });

  it("sorts all Ask-selected tasks by numeric progress and ETA before pagination, with unknown values last", async () => {
    const tasks: TaskLite[] = Array.from({ length: 30 }, (_, i) => ({
      task_id: `task-${i + 1}`, name: `Task ${i + 1}`, owner_id: "owner", status: "grading",
      workflow_revision: 1, problem_count: 4, student_count: 1, kb_docs: {}, kb_doc_count: 0,
      created_at: 1, updated_at: 1, progress_percent: i + 1, eta_seconds: (30 - i) * 60,
    }));
    tasks.push({ ...tasks[0], task_id: "unknown", name: "Unknown", progress_percent: null, eta_seconds: null });
    mocks.interpret.mockResolvedValue({ ...interpretation, execution: {
      recognized: true, kind: "tasks", explanation: "任务查询完成", data: { columns: [], rows: [] },
      selection: { kind: "tasks", ids: tasks.map(task => task.task_id) }, tasks, chart: null,
    } });
    mount(); ask();
    await screen.findByText("任务查询完成");
    const names = () => within(screen.getByRole("table")).getAllByRole("row").slice(1)
      .map(row => within(row).getAllByRole("link")[0].textContent);
    fireEvent.click(screen.getByRole("button", { name: /^进度，/ }));
    expect(names()).toEqual(tasks.slice(0, 25).map(task => task.name));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(names()).toEqual(["Task 26", "Task 27", "Task 28", "Task 29", "Task 30", "Unknown"]);
    fireEvent.click(screen.getByRole("button", { name: /^进度，/ }));
    expect(names()[0]).toBe("Task 30");
    expect(names()).toHaveLength(25);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(names()).toEqual(["Task 5", "Task 4", "Task 3", "Task 2", "Task 1", "Unknown"]);
    fireEvent.click(screen.getByRole("button", { name: /^预计剩余，/ }));
    expect(names()[0]).toBe("Task 30");
    expect(screen.getByText("约 1 分钟")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^预计剩余，/ }));
    expect(names()[0]).toBe("Task 1");
    expect(screen.getByText("约 30 分钟")).toBeInTheDocument();
    expect(tasks[0].task_id).toBe("task-1");
  });

  it("refreshes all selected active tasks before sorting, stops completed polling, and retains failed snapshots", async () => {
    vi.useFakeTimers();
    const tasks: TaskLite[] = Array.from({ length: 30 }, (_, i) => ({
      task_id: `task-${i + 1}`, name: `Task ${i + 1}`, owner_id: "owner", status: "grading",
      workflow_revision: 1, problem_count: 4, student_count: 1, kb_docs: {}, kb_doc_count: 0,
      created_at: 1, updated_at: 1,
      progress_percent: i + 1, eta_seconds: (30 - i) * 60,
    }));
    tasks.push({ ...tasks[0], task_id: "completed", name: "Completed", status: "finalized", progress_percent: 100, eta_seconds: 0 });
    const states = new Map(tasks.map(task => [task.task_id, { ...task }]));
    mocks.state.mockImplementation(async (taskId: string) => {
      if (taskId === "task-2") throw new Error("Temporary state failure");
      return states.get(taskId);
    });
    mocks.interpret.mockResolvedValue({ ...interpretation, execution: {
      recognized: true, kind: "tasks", explanation: "任务查询完成", data: { columns: [], rows: [] },
      selection: { kind: "tasks", ids: tasks.map(task => task.task_id) }, tasks, chart: null,
    } });
    const client = mount(); ask();
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });
    fireEvent.click(screen.getByRole("button", { name: /^进度，/ }));
    const rows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);
    const names = () => rows().map(row => within(row).getAllByRole("link")[0].textContent);
    expect(names()[0]).toBe("Task 1");
    expect(names()).not.toContain("Task 30");
    expect(mocks.state).toHaveBeenCalledWith("task-30");
    expect(mocks.state).not.toHaveBeenCalledWith("completed");
    expect(within(rows()[1]).getByText("2%")).toBeInTheDocument();

    states.set("task-30", { ...tasks[29], progress_percent: 0, eta_seconds: 5 });
    states.set("task-1", { ...tasks[0], status: "graded", progress_percent: 100, eta_seconds: 0 });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_010); });
    expect(names()[0]).toBe("Task 30");
    expect(within(rows()[0]).getByText("0%")).toBeInTheDocument();
    expect(within(rows()[0]).getByText("约 5 秒")).toBeInTheDocument();
    expect(client.getQueryData(["tasks", "state", "task-1"])).toMatchObject({ status: "graded" });
    const completedCalls = mocks.state.mock.calls.filter(([id]) => id === "task-1").length;
    await act(async () => { await vi.advanceTimersByTimeAsync(3_010); });
    expect(mocks.state.mock.calls.filter(([id]) => id === "task-1")).toHaveLength(completedCalls);

    const callsBeforeClear = mocks.state.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "清空筛选" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_010); });
    expect(mocks.state).toHaveBeenCalledTimes(callsBeforeClear);
    expect(tasks[0].status).toBe("grading");
    expect(tasks[29].progress_percent).toBe(30);
  });
});

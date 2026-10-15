import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { LocalDraftManager } from "./LocalDraftManager";

const mocks = vi.hoisted(() => ({ list: vi.fn(), tasks: vi.fn(), remove: vi.fn(), owner: "teacher-a", listeners: new Set<(event?: Event) => void>() }));
vi.mock("@/api/tasks", () => ({ listTasks: mocks.tasks }));
vi.mock("@/hooks/useDraftProtection", () => ({ useDraftOwner: () => mocks.owner }));
vi.mock("@/lib/pageDraftStore", () => ({
  listPageDrafts: mocks.list,
  removePageDraft: mocks.remove,
  draftError: (error: Error) => error.message,
  subscribeDraftChanges: (listener: (event?: Event) => void) => { mocks.listeners.add(listener); return () => mocks.listeners.delete(listener); },
}));

const DAY = 24 * 60 * 60 * 1000;
function draft(id: string, expiresIn: number) {
  return { scope: `metadata:${id}`, title: `Task ${id}`, expires: Date.now() + expiresIn, savedAt: Date.now() + expiresIn - 7 * DAY, bytes: 1024 };
}
function view(props: Parameters<typeof LocalDraftManager>[0] = {}) {
  return <I18nProvider><MemoryRouter><LocalDraftManager {...props} /></MemoryRouter></I18nProvider>;
}

describe("saved draft list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.owner = "teacher-a";
    mocks.tasks.mockResolvedValue({});
    mocks.listeners.clear();
    window.localStorage.removeItem("smartai_locale");
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

  it("shows five drafts with imminent expiry first, then recent saves, and links to the full history", async () => {
    mocks.list.mockResolvedValue([
      draft("old", 3 * DAY), draft("recent", 7 * DAY), draft("next", 4 * DAY),
      draft("soon", DAY), draft("later", 5 * DAY), draft("first", 60_000), draft("expired", -1),
    ]);
    render(view({ maxItems: 5, showViewAll: true }));
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map(row => within(row).getByRole("link").textContent)).toEqual([
      "打开草稿 · Task first · 任务信息", "打开草稿 · Task soon · 任务信息", "打开草稿 · Task recent · 任务信息",
      "打开草稿 · Task later · 任务信息", "打开草稿 · Task next · 任务信息",
    ]);
    expect(within(rows[0]).getByText(/即将清理/)).toBeInTheDocument();
    expect(within(rows[2]).queryByText(/即将清理/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看全部草稿" })).toHaveAttribute("href", "/history?view=drafts");
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", "/tasks/first/edit");
    expect(mocks.list).toHaveBeenCalledExactlyOnceWith("teacher-a");
  });

  it("never exposes opaque task or object identifiers when names are unavailable", async () => {
    const row = { ...draft("389876bff62b", DAY), scope: "question:389876bff62b:opaque-question-id:scoring", title: undefined };
    mocks.list.mockResolvedValue([row]);
    mocks.tasks.mockReturnValue(new Promise(() => {}));
    render(view());
    const link = await screen.findByRole("link", { name: /任务名称暂不可用 · 题目资料编辑/ });
    expect(link).toHaveAttribute("href", "/tasks/389876bff62b/questions/opaque-question-id/content");
    expect(screen.queryByText(/389876bff62b|opaque-question-id/)).not.toBeInTheDocument();
    expect(screen.getByText(/暂存于/)).toBeInTheDocument();
  });
  it("shows the current task name, page and saved time without internal identifiers", async () => {
    mocks.list.mockResolvedValue([{ ...draft("389876bff62b", DAY), scope: "grading:389876bff62b:389876bff62b", title: undefined }]);
    mocks.tasks.mockResolvedValue({ task: { task_id: "389876bff62b", name: "微积分周测" } });
    render(view());
    expect(await screen.findByRole("link", { name: "打开草稿 · 微积分周测 · 批改设置" })).toBeInTheDocument();
    expect(screen.getByText(/暂存于/)).toBeInTheDocument();
    expect(screen.queryByText(/389876bff62b/)).not.toBeInTheDocument();
  });

  it("shows the complete list without a limit and refreshes it after confirmed deletion", async () => {
    const rows = Array.from({ length: 6 }, (_, index) => draft(`task-${index}`, (index + 1) * DAY));
    mocks.list.mockImplementation(async () => [...rows]);
    mocks.remove.mockImplementation(async (owner: string, scope: string) => {
      rows.splice(rows.findIndex(row => row.scope === scope), 1);
      mocks.listeners.forEach(listener => listener());
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(view());
    expect(await screen.findAllByRole("listitem")).toHaveLength(6);
    fireEvent.click(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(5));
    expect(mocks.remove).toHaveBeenCalledExactlyOnceWith("teacher-a", "metadata:task-0");
  });

  it("removes expired rows while the page remains open", async () => {
    vi.useFakeTimers();
    mocks.list.mockResolvedValue([draft("expires", 1_000), draft("stays", 5 * DAY)]);
    render(view());
    await act(async () => {});
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /stays/ })).toBeInTheDocument();
  });

  it("does not display a previous owner's rows while a new owner's read is pending", async () => {
    mocks.list.mockResolvedValueOnce([draft("private", DAY)]).mockReturnValueOnce(new Promise(() => {}));
    const { rerender } = render(view());
    await screen.findByRole("link", { name: /private/ });
    mocks.owner = "teacher-b";
    rerender(view());
    expect(screen.queryByRole("link", { name: /private/ })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(mocks.list).toHaveBeenLastCalledWith("teacher-b");
  });

  it("clears immediately on session cleanup and ignores an earlier pending read", async () => {
    const rows = [draft("private", DAY)];
    let finish!: (value: typeof rows) => void;
    mocks.list.mockResolvedValueOnce(rows).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render(view());
    await screen.findByRole("link", { name: /private/ });
    act(() => { mocks.listeners.forEach(listener => listener()); });
    act(() => { mocks.listeners.forEach(listener => listener(new CustomEvent("change", { detail: { type: "clear" } }))); });
    expect(screen.queryByRole("link", { name: /private/ })).not.toBeInTheDocument();
    await act(async () => { finish(rows); });
    expect(screen.queryByRole("link", { name: /private/ })).not.toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledTimes(2);
  });
});

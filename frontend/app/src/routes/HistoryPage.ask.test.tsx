import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { HistoryInterpretation } from "@/types";
import { HistoryPage } from "./HistoryPage";

const mocks = vi.hoisted(() => ({ history: vi.fn(), interpret: vi.fn() }));
vi.mock("@/api/hooks", () => ({
  useTaskHistory: mocks.history,
  useTags: () => ({ data: [] }),
  useDeleteTask: () => ({ isPending: false }),
  useInterpretTaskHistoryQuery: () => ({ mutateAsync: mocks.interpret, isPending: false, reset: vi.fn() }),
}));

function mount() {
  render(<I18nProvider><MemoryRouter><HistoryPage /></MemoryRouter></I18nProvider>);
}
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
});

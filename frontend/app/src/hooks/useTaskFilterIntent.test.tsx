import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import { EMPTY_FILTER_INTENT, parseLocalTaskFilter } from "@/lib/taskFilterIntent";
import type { FilterIntentResult } from "@/types";
import { useTaskFilterIntent } from "./useTaskFilterIntent";
vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));
const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;
const deferred = () => {
  let resolve!: (result: FilterIntentResult) => void;
  return { promise: new Promise<FilterIntentResult>((next) => { resolve = next; }), resolve: (value: FilterIntentResult) => resolve(value) };
};
function useHarness(context = "S1") {
  const [params, setParams] = useSearchParams();
  const filter = useTaskFilterIntent({ taskId: "task", surface: "question_preparation", contextKey: context,
    resolveLocal: (value) => parseLocalTaskFilter(value, "question_preparation") });
  return { ...filter, params, header: () => setParams((current) => { const next = new URLSearchParams(current); next.set("column_sort", "number:desc"); return next; }) };
}

describe("Ask request ownership and ordering", () => {
  beforeEach(() => vi.clearAllMocks());
  it("does not call a model for a just-submitted local instruction before URL rerender", async () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    await act(async () => { await result.current.apply("按满分升序"); });
    expect(result.current.intent?.sort).toBe("max_score_asc");
    expect(result.current.params.get("sort")).toBe("max_score_asc");
    expect(interpretFilterIntent).not.toHaveBeenCalled();
  });
  it("lets an explicitly resubmitted natural-language order supersede a table header", async () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    await act(async () => { await result.current.apply("按满分升序"); });
    act(() => result.current.header());
    expect(result.current.params.get("column_sort")).toBe("number:desc");
    await act(async () => { await result.current.apply("按满分升序"); });
    expect(result.current.params.get("column_sort")).toBeNull();
    expect(result.current.params.get("sort")).toBe("max_score_asc");
  });
  it("aborts and ignores a late semantic response after a header changes", async () => {
    const pending = deferred(); vi.mocked(interpretFilterIntent).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useHarness(), { wrapper });
    act(() => { void result.current.apply("please find the unusual records"); });
    await waitFor(() => expect(result.current.pending).toBe(true));
    const signal = vi.mocked(interpretFilterIntent).mock.calls[0][3];
    act(() => result.current.header());
    await waitFor(() => expect(signal?.aborted).toBe(true));
    await act(async () => pending.resolve({ ...EMPTY_FILTER_INTENT, sort: "max_score_desc" }));
    expect(result.current.params.get("column_sort")).toBe("number:desc");
    expect(result.current.intent).toBeNull();
  });
  it("ignores a previous student's response after the detail context changes", async () => {
    const pending = deferred(); vi.mocked(interpretFilterIntent).mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(({ student }) => useHarness(student), { initialProps: { student: "S1" }, wrapper });
    act(() => { void result.current.apply("find the unusual records"); });
    rerender({ student: "S2" });
    await act(async () => pending.resolve({ ...EMPTY_FILTER_INTENT, sort: "max_score_desc" }));
    expect(result.current.intent).toBeNull();
    expect(result.current.pending).toBe(false);
  });
  it("does not resurrect an old response after the text is cleared and retyped", async () => {
    const pending = deferred(); vi.mocked(interpretFilterIntent).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useHarness(), { wrapper });
    act(() => { void result.current.apply("find unusual records"); });
    act(() => result.current.setQuery(""));
    act(() => result.current.setQuery("find unusual records"));
    await act(async () => pending.resolve({ ...EMPTY_FILTER_INTENT, sort: "max_score_desc" }));
    expect(result.current.intent).toBeNull();
  });
  it("keeps an equivalent local intent stable during unrelated renders", () => {
    const { result, rerender } = renderHook(() => useHarness(), { wrapper });
    const intent = result.current.intent;
    rerender();
    expect(result.current.intent).toBe(intent);
  });
  it("rejects a valid-looking model response intended for another screen", async () => {
    vi.mocked(interpretFilterIntent).mockResolvedValue({ ...EMPTY_FILTER_INTENT, sort: "name_asc" });
    const { result } = renderHook(() => useHarness(), { wrapper });
    await act(async () => { await result.current.apply("order it my way"); });
    expect(result.current.unrecognized).toBe(true);
    expect(result.current.intent).toBeNull();
    expect(result.current.params.has("sort")).toBe(false);
  });
});

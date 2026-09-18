import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { interpretFilterIntent } from "@/api/analytics";
import { EMPTY_FILTER_INTENT, parseLocalTaskFilter } from "@/lib/taskFilterIntent";
import { resolveResultQuestionQuery } from "@/routes/tasks/results/QuestionAnalysisOverview";
import { useTaskFilterIntent } from "./useTaskFilterIntent";
vi.mock("@/api/analytics", () => ({ interpretFilterIntent: vi.fn() }));
const options = { taskId: "audit-task", surface: "question_preparation" as const,
  resolveLocal: (value: string) => parseLocalTaskFilter(value, "question_preparation") };
function wrapper(client: QueryClient, query = "") {
  return ({children}: {children: ReactNode}) => <QueryClientProvider client={client}><MemoryRouter initialEntries={[`/?q=${encodeURIComponent(query)}`]}>{children}</MemoryRouter></QueryClientProvider>;
}
describe("Ask audit regressions", () => {
  beforeEach(() => vi.clearAllMocks());
  it("does not locally turn an exclusive lower bound into an inclusive one", async () => {
    const client = new QueryClient();
    vi.mocked(interpretFilterIntent).mockResolvedValue({...EMPTY_FILTER_INTENT, recognized:false});
    const {result} = renderHook(() => useTaskFilterIntent({taskId:"audit-task", surface:"question_analysis",
      resolveLocal: (text) => resolveResultQuestionQuery([], text, "zh-CN")}), {wrapper: wrapper(client)});
    act(() => result.current.setQuery("得分率高于90%"));
    expect(result.current.intent).toBeNull();
    await act(async () => {await result.current.apply();});
    expect(interpretFilterIntent).toHaveBeenCalledTimes(1);
    expect(result.current.intent).toBeNull();
    client.clear();
  });
  it("retains an accepted instruction when returning from a detail page without another model call", async () => {
    const client = new QueryClient();
    const text = "请帮我找找还没有标准答案的那些题";
    vi.mocked(interpretFilterIntent).mockResolvedValue({...EMPTY_FILTER_INTENT, material_field:"answer", material_status:"missing"});
    const first = renderHook(() => useTaskFilterIntent(options), {wrapper: wrapper(client)});
    await act(async () => {await first.result.current.apply(text);});
    expect(first.result.current.intent?.material_status).toBe("missing");
    first.unmount();
    const second = renderHook(() => useTaskFilterIntent(options), {wrapper: wrapper(client, text)});
    expect(second.result.current.intent?.material_status).toBe("missing");
    expect(interpretFilterIntent).toHaveBeenCalledTimes(1);
    second.unmount(); client.clear();
  });
  it("does not restore an accepted instruction after clearing and retyping it", async () => {
    const client = new QueryClient(); const text="请帮我找找没有答案的题";
    vi.mocked(interpretFilterIntent).mockResolvedValue({...EMPTY_FILTER_INTENT, material_field:"answer", material_status:"missing"});
    const {result,unmount}=renderHook(()=>useTaskFilterIntent(options), {wrapper:wrapper(client)});
    await act(async()=>{await result.current.apply(text);});
    act(()=>result.current.setQuery("")); act(()=>result.current.setQuery(text));
    expect(result.current.intent).toBeNull();
    unmount(); client.clear();
  });
});

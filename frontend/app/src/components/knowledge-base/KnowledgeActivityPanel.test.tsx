import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { KnowledgeActivityPanel } from "./KnowledgeActivityPanel";
import { knowledgeProgress, knowledgeStatusLabel } from "@/lib/knowledgeProgress";
import type { KnowledgeActivityResponse } from "@/types/personalKnowledge";

const api = vi.hoisted(() => ({ getJSON: vi.fn(), postJSON: vi.fn() }));
vi.mock("@/api/client", () => api);
const clients: QueryClient[] = [];
function response(status = "processing", active = 1): KnowledgeActivityResponse {
  return { items: [{ id: "book", title: "抽象代数教材", original_name: "algebra.pdf", activity_status: status,
    status: "partial", created_at: 1, updated_at: 2, sha256: "digest", size_bytes: 100,
    parser_version: "v1", chunk_count: 24, content_version: "run",
    ingestion: { id: "run", status, total_pages: 100, processed_pages: 24, searchable_pages: 24, failed_pages: 0 },
  }], total: 1, active_count: active, counts: { active }, page: 1, page_size: 20 };
}
function mount(compact = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><I18nProvider><MemoryRouter><KnowledgeActivityPanel compact={compact} /></MemoryRouter></I18nProvider></QueryClientProvider>);
}
beforeEach(() => { api.getJSON.mockReset(); api.postJSON.mockReset(); localStorage.setItem("smartai_locale", "zh-CN"); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.useRealTimers(); });

it("shows the book and processed count on the dashboard with a history link, without per-row fetches", async () => {
  api.getJSON.mockResolvedValue(response());
  mount(true);
  await screen.findByText("抽象代数教材");
  expect(screen.getByText("识别中")).toBeInTheDocument();
  expect(screen.getByText("已处理 24/100 页")).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "24");
  expect(screen.getByRole("link", { name: "全部记录" })).toHaveAttribute("href", "/history?view=knowledge");
  expect(api.getJSON).toHaveBeenCalledTimes(1);
  expect(api.postJSON).not.toHaveBeenCalled();
});

it("never turns 100 percent processed with failed pages into recognition complete", async () => {
  const data = response("partial", 0);
  Object.assign(data.items[0].ingestion!, { processed_pages: 100, failed_pages: 76 });
  api.getJSON.mockResolvedValue(data);
  mount();
  await screen.findByText("部分可用，未全部完成");
  expect(screen.getByText("100%")).toBeInTheDocument();
  expect(screen.queryByText("识别完成")).not.toBeInTheDocument();
});

it("shows current retry and old searchable version without inventing a page percentage", async () => {
  const data = response("queued");
  data.items[0].status = "ready";
  data.items[0].content_version = "older";
  data.items[0].ingestion = { id: "retry", status: "queued", total_pages: null };
  api.getJSON.mockResolvedValue(data);
  mount();
  await screen.findByText("等待识别");
  expect(screen.getByText("仍可检索此前已保存版本")).toBeInTheDocument();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
});

it("polls a single metadata feed and stops when all active work finishes", async () => {
  vi.useFakeTimers();
  api.getJSON.mockResolvedValueOnce(response()).mockResolvedValue(response("complete", 0));
  mount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  await act(async () => { await vi.advanceTimersByTimeAsync(5010); });
  expect(screen.getByText("识别完成")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(api.getJSON).toHaveBeenCalledTimes(2);
  expect(api.getJSON.mock.calls.every(([url]) => url.startsWith("/knowledge/activity?"))).toBe(true);
  expect(api.postJSON).not.toHaveBeenCalled();
});

it("filters on the server and debounces textbook-name search", async () => {
  api.getJSON.mockResolvedValue(response());
  mount();
  await screen.findByText("抽象代数教材");
  fireEvent.change(screen.getByRole("combobox", { name: "识别状态" }), { target: { value: "attention" } });
  await waitFor(() => expect(api.getJSON.mock.calls.at(-1)?.[0]).toContain("state=attention"));
  fireEvent.change(screen.getByRole("textbox", { name: "搜索教材" }), { target: { value: "algebra" } });
  await waitFor(() => expect(api.getJSON.mock.calls.at(-1)?.[0]).toContain("q=algebra"));
});

it("keeps a previously read snapshot visible when refresh fails", async () => {
  api.getJSON.mockResolvedValueOnce(response("paused", 0)).mockRejectedValue(new Error("offline"));
  mount();
  await screen.findByText("抽象代数教材");
  fireEvent.click(screen.getByRole("button", { name: "刷新教材进度" }));
  await screen.findByRole("alert");
  expect(screen.getByText("抽象代数教材")).toBeInTheDocument();
  expect(api.postJSON).not.toHaveBeenCalled();
});

it("treats unknown page totals and unknown statuses honestly", () => {
  expect(knowledgeProgress({ total_pages: null })).toBeNull();
  expect(knowledgeProgress({ total_pages: 100, processed_pages: 900 })?.percent).toBe(100);
  expect(knowledgeStatusLabel("surprise", true)).toBe("状态待确认");
});

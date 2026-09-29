import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeIngestionStatus } from "./KnowledgeIngestionStatus";

const api = vi.hoisted(() => ({ getJSON: vi.fn(), postJSON: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({ ...await importOriginal<typeof import("@/api/client")>(), ...api }));
beforeEach(() => { api.getJSON.mockReset(); api.postJSON.mockReset(); });

it("does not label a partial book as fully parsed, and loads coverage only on demand", async () => {
  const summary = { id: "run", status: "partial", total_pages: 25, processed_pages: 25, searchable_pages: 24, failed_pages: 1 };
  api.getJSON.mockResolvedValue({ summary, pages: [{ page_number: 25, state: "failed", error_code: "provider_submit_uncertain" }], next_offset: null });
  render(<KnowledgeIngestionStatus documentId="book" status="partial" ingestion={summary} zh />);
  expect(api.getJSON).not.toHaveBeenCalled();
  expect(screen.queryByText("已解析")).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("部分可检索 · 25/25"));
  await screen.findByText("25: 内容不完整 · 模型请求状态无法确认");
  expect(api.getJSON).toHaveBeenCalledWith("/knowledge/documents/book/coverage?offset=0&limit=20");
});

it("resumes a paused ingestion through an explicit command, not through its status GET", async () => {
  const summary = { id: "run", status: "paused", total_pages: 1000, processed_pages: 24 };
  api.getJSON.mockResolvedValue({ summary, pages: [], next_offset: null });
  api.postJSON.mockResolvedValue({ status: "queued" });
  render(<KnowledgeIngestionStatus documentId="book" status="partial" ingestion={summary} zh />);
  await userEvent.click(screen.getByText("已暂停 · 24/1000"));
  await waitFor(() => expect(screen.getByRole("button", { name: "继续处理" })).not.toBeDisabled());
  expect(api.postJSON).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "继续处理" }));
  expect(api.postJSON).toHaveBeenCalledWith("/knowledge/documents/book/resume", {});
});

it("refreshes an active retry and stops polling when its current summary is terminal", async () => {
  vi.useFakeTimers();
  try {
    const summary = { id: "retry", status: "queued", processed_pages: 0 };
    api.getJSON.mockResolvedValue({ summary: { ...summary, status: "partial", total_pages: 110,
      processed_pages: 110, searchable_pages: 110, partially_searchable_pages: 110, failed_pages: 110 }, pages: [], next_offset: null });
    const view = render(<KnowledgeIngestionStatus documentId="book" status="failed" ingestion={summary} zh />);
    expect(api.getJSON).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText("部分可检索 · 110/110")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(api.getJSON).toHaveBeenCalledTimes(1);
    expect(api.postJSON).not.toHaveBeenCalled();
    view.unmount();
  } finally { vi.useRealTimers(); }
});

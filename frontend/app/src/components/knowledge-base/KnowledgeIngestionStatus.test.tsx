import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeIngestionStatus } from "./KnowledgeIngestionStatus";

const api = vi.hoisted(() => ({ getJSON: vi.fn(), postJSON: vi.fn() }));
vi.mock("@/api/client", () => api);
beforeEach(() => { api.getJSON.mockReset(); api.postJSON.mockReset(); });

it("does not label a partial book as fully parsed, and loads coverage only on demand", async () => {
  const summary = { id: "run", status: "partial", total_pages: 25, processed_pages: 25, searchable_pages: 24, failed_pages: 1 };
  api.getJSON.mockResolvedValue({ summary, pages: [{ page_number: 25, state: "failed", error_code: "provider_submit_uncertain" }], next_offset: null });
  render(<KnowledgeIngestionStatus documentId="book" status="partial" ingestion={summary} zh />);
  expect(api.getJSON).not.toHaveBeenCalled();
  expect(screen.queryByText("已解析")).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("部分可检索 · 25/25"));
  await screen.findByText("25: failed · provider_submit_uncertain");
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

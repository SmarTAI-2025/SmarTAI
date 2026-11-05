import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeIngestionStatus } from "./KnowledgeIngestionStatus";

const api = vi.hoisted(() => ({ getJSON: vi.fn(), postJSON: vi.fn(), postMultipart: vi.fn() }));
const models = vi.hoisted(() => ({ listStageProviders: vi.fn() }));
vi.mock("@/api/experts", () => models);
vi.mock("@/api/client", async (importOriginal) => ({ ...await importOriginal<typeof import("@/api/client")>(), ...api }));
beforeEach(() => { api.getJSON.mockReset(); api.postJSON.mockReset(); api.postMultipart.mockReset(); models.listStageProviders.mockReset().mockResolvedValue([]); });

it("requires opt-in to resubmit unknown requests and resets it after retry", async () => {
  const summary = { id: "run", status: "partial", total_pages: 1, processed_pages: 1, failed_pages: 1 };
  api.getJSON.mockResolvedValue({ summary, pages: [], next_offset: null });
  render(<KnowledgeIngestionStatus documentId="book" ingestion={summary} zh />);
  await userEvent.click(screen.getByText("部分可检索 · 1/1"));
  const checkbox = screen.getByRole("checkbox");
  expect(checkbox).not.toBeChecked();
  await waitFor(() => expect(screen.getByRole("button", { name: "重试缺页" })).not.toBeDisabled());
  await userEvent.click(screen.getByRole("button", { name: "重试缺页" }));
  expect(api.postMultipart).toHaveBeenLastCalledWith("/knowledge/documents/book/retry-failed", null,
    { fields: { accept_uncertain_resubmission: false } });
  await userEvent.click(checkbox);
  await userEvent.click(screen.getByRole("button", { name: "重试缺页" }));
  expect(api.postMultipart).toHaveBeenLastCalledWith("/knowledge/documents/book/retry-failed", null,
    { fields: { accept_uncertain_resubmission: true } });
  await waitFor(() => expect(checkbox).not.toBeChecked());
});

it("does not label a partial book as fully parsed, and loads coverage only on demand", async () => {
  const summary = { id: "run", status: "partial", total_pages: 25, processed_pages: 25, searchable_pages: 24, failed_pages: 1 };
  api.getJSON.mockResolvedValue({ summary, pages: [{ page_number: 25, state: "failed", error_code: "provider_submit_uncertain" }], next_offset: null });
  render(<KnowledgeIngestionStatus documentId="book" status="partial" ingestion={summary} zh />);
  expect(api.getJSON).not.toHaveBeenCalled();
  expect(screen.queryByText("已解析")).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("部分可检索 · 25/25"));
  await screen.findByText("25: 内容不完整 · 模型调用未完成，重试需确认");
  expect(api.getJSON).toHaveBeenCalledWith("/knowledge/documents/book/coverage?offset=0&limit=20&issues_only=true");
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

it.each([true, false])("only lists gaps in a long book and sends the selected retry model (zh=%s)", async (zh) => {
  const summary = { id: "run", status: "partial", total_pages: 100, processed_pages: 100, searchable_pages: 99, failed_pages: 1 };
  models.listStageProviders.mockResolvedValue([
    { provider_id: "gemini", provider_type: "gemini", model: "gemini-test", enabled: true },
    { provider_id: "replacement", provider_type: "zhipu", model: "glm-test", enabled: true },
    { provider_id: "disabled", provider_type: "openai", model: "disabled-model", enabled: false },
  ]);
  api.getJSON.mockResolvedValue({ summary, pages: [
    { page_number: 1, state: "searchable" }, { page_number: 2, state: "searchable_with_warning" },
    { page_number: 3, state: "blank_confirmed" }, { page_number: 99, state: "failed" },
  ], next_offset: null });
  render(<KnowledgeIngestionStatus documentId="book" ingestion={summary} zh={zh} />);
  expect(models.listStageProviders).not.toHaveBeenCalled();
  await userEvent.click(screen.getByText(zh ? "部分可检索 · 100/100" : "Partial coverage · 100/100"));
  const select = await screen.findByRole("combobox", { name: /重试模型|Retry model/ });
  await screen.findByRole("option", { name: /glm-test/ });
  expect(screen.queryByRole("option", { name: /disabled-model/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/^1:|^2:|^3:/)).not.toBeInTheDocument();
  expect(screen.getByText(zh ? "99: 内容不完整" : "99: Incomplete")).toBeInTheDocument();
  expect(api.getJSON).toHaveBeenCalledWith("/knowledge/documents/book/coverage?offset=0&limit=20&issues_only=true");
  await userEvent.selectOptions(select, "replacement");
  expect(api.postMultipart).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: zh ? "重试缺页" : "Retry gaps" }));
  expect(api.postMultipart).toHaveBeenCalledWith("/knowledge/documents/book/retry-failed", null,
    { fields: { recognition_route_id: "replacement", accept_uncertain_resubmission: false } });
});

it.each(["complete", "complete_with_warning"])("does not expand a successfully processed document (%s)", async (status) => {
  const view = render(<KnowledgeIngestionStatus documentId="book" ingestion={{ id: "run", status,
    total_pages: 100, processed_pages: 100, searchable_pages: 100, warning_pages: status === "complete" ? 0 : 100 }} zh />);
  expect(view.container.querySelector("details")).toBeNull();
  expect(api.getJSON).not.toHaveBeenCalled();
  expect(models.listStageProviders).not.toHaveBeenCalled();
});

it("keeps a failed page visible when the model list is unavailable", async () => {
  const summary = { id: "run", status: "failed", failed_pages: 1 };
  api.getJSON.mockResolvedValue({ summary, pages: [{ page_number: 1, state: "failed" }], next_offset: null });
  models.listStageProviders.mockRejectedValue(new Error("offline"));
  render(<KnowledgeIngestionStatus documentId="book" ingestion={summary} zh />);
  await userEvent.click(screen.getByText("处理未完成"));
  await screen.findByText("模型列表暂不可用，请重新展开。");
  expect(screen.getByText("1: 内容不完整")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重试缺页" })).not.toBeDisabled();
});

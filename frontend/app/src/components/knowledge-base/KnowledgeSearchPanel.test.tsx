import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeSearchPanel } from "./KnowledgeSearchPanel";

const api = vi.hoisted(() => ({ postJSON: vi.fn() }));
vi.mock("@/api/client", () => api);
vi.mock("@/api/experts", () => ({ listExperts: async () => [{ provider_id: "owner-model", provider_type:"zhipu", model:"test-model", enabled:true, scope:"owner" }] }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US" }) }));
vi.mock("./KnowledgeCitationPreview", () => ({ KnowledgeCitationPreview: () => <span>Citation</span> }));
beforeEach(() => api.postJSON.mockReset());

it("searches inside a settings form without submitting the settings", async () => {
  api.postJSON.mockResolvedValue({ matches: [] });
  const saveSettings = vi.fn((event) => event.preventDefault());
  render(<form onSubmit={saveSettings}><KnowledgeSearchPanel documentIds={["book"]} /></form>);
  await userEvent.type(screen.getByRole("textbox"), "even order{Enter}");
  expect(await screen.findByRole("status")).toHaveTextContent("No matching passages");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(api.postJSON).toHaveBeenCalledTimes(2);
  expect(saveSettings).not.toHaveBeenCalled();
  expect(document.querySelectorAll("form")).toHaveLength(1);
});

it("searches selected materials only on submission and reports an honest empty result", async () => {
  api.postJSON.mockResolvedValue({ matches: [] });
  render(<KnowledgeSearchPanel documentIds={["book"]} />);
  expect(api.postJSON).not.toHaveBeenCalled();
  await userEvent.type(screen.getByRole("textbox"), "group homomorphism");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByRole("status")).toHaveTextContent("No matching passages");
  expect(api.postJSON).toHaveBeenCalledWith("/knowledge/search", { query: "group homomorphism", document_ids: ["book"], limit: 5 });
});

it("discards a response after the material selection changes", async () => {
  let resolve!: (value: unknown) => void;
  api.postJSON.mockReturnValue(new Promise((done) => { resolve = done; }));
  const view = render(<KnowledgeSearchPanel documentIds={["old"]} />);
  await userEvent.type(screen.getByRole("textbox"), "algebra");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  view.rerender(<KnowledgeSearchPanel documentIds={["new"]} />);
  await act(async () => resolve({ matches: [{ content: "stale", citation: { citation_id: "old" } }] }));
  expect(screen.queryByText("stale")).not.toBeInTheDocument();
});

it("only requests the explicitly selected model and keeps fallback visible", async () => {
  api.postJSON.mockResolvedValue({ matches: [], query_plan: { status: "rewrite_unavailable", provider_calls: 0, cached: true } });
  render(<KnowledgeSearchPanel documentIds={["book"]} />);
  await screen.findByRole("option", { name: /Model assisted:/ });
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Search mode" }), "owner-model");
  expect(screen.getByText(/Only your query is sent/)).toBeInTheDocument();
  await userEvent.type(screen.getByRole("textbox"), "中文查询");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("showing local search results");
  expect(screen.getByText(/Model calls this search: 0/)).toHaveTextContent("Reused query");
  expect(api.postJSON).toHaveBeenCalledWith("/knowledge/search", { query:"中文查询", document_ids:["book"], limit:5, query_provider_id:"owner-model" });
});

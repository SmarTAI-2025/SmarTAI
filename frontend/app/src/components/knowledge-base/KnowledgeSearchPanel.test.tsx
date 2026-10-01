import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeSearchPanel } from "./KnowledgeSearchPanel";

const api = vi.hoisted(() => ({ postJSON: vi.fn() }));
vi.mock("@/api/client", () => api);
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US" }) }));
vi.mock("./KnowledgeCitationPreview", () => ({ KnowledgeCitationPreview: () => <span>Citation</span> }));
beforeEach(() => api.postJSON.mockReset());

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

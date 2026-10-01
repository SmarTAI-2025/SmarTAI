import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { KnowledgeCitationPreview } from "./KnowledgeCitationPreview";

const api = vi.hoisted(() => ({ getJSON: vi.fn(), getBlob: vi.fn() }));
vi.mock("@/api/client", () => api);
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "en-US", t: (key: string) => key }) }));
vi.mock("@/components/tasks/OriginalFilePreviewPanel", () => ({ OriginalFilePreviewPanel: ({ initialPage, loadState }: { initialPage: number; loadState: string }) => <span>{`Page ${initialPage}: ${loadState}`}</span> }));
const citation = { citation_id: "kb:chunk", chunk_id: "chunk", document_id: "book", content_version: "v1", source_sha256: "sha", original_name: "Book.pdf", unit: "page", page_number: 801 };
beforeEach(() => { api.getJSON.mockReset(); api.getBlob.mockReset(); });

it("shows partial or unverified evidence before opening the original without a request", () => {
  const view = render(<KnowledgeCitationPreview citation={{ ...citation, coverage_complete: false }} />);
  expect(screen.getByText("Incomplete coverage or unverified recognition.")).toBeInTheDocument();
  view.rerender(<KnowledgeCitationPreview citation={{ ...citation, coverage_complete: true, confidence: "unverified" }} />);
  expect(screen.getByText("Incomplete coverage or unverified recognition.")).toBeInTheDocument();
  view.rerender(<KnowledgeCitationPreview citation={{ ...citation, coverage_complete: true, confidence: "high" }} />);
  expect(screen.queryByText("Incomplete coverage or unverified recognition.")).not.toBeInTheDocument();
  expect(api.getJSON).not.toHaveBeenCalled();
  expect(api.getBlob).not.toHaveBeenCalled();
});

it("loads only on demand and refuses a changed citation before fetching its original", async () => {
  api.getJSON.mockResolvedValue({ content: "wrong version", content_version: "v2", source_sha256: "sha" });
  render(<KnowledgeCitationPreview citation={citation} />);
  expect(api.getJSON).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button"));
  expect(await screen.findByText("Page 801: error")).toBeInTheDocument();
  expect(api.getBlob).not.toHaveBeenCalled();
  expect(screen.queryByText("wrong version")).not.toBeInTheDocument();
});

it("opens the original at its cited page and revokes the blob on close", async () => {
  api.getJSON.mockResolvedValue({ content: "exact passage", content_version: "v1", source_sha256: "sha" });
  api.getBlob.mockResolvedValue(new Blob(["synthetic"]));
  const create = vi.fn(() => "blob:original");
  const revoke = vi.fn();
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
  const view = render(<KnowledgeCitationPreview citation={citation} />);
  await userEvent.click(screen.getByRole("button"));
  await waitFor(() => expect(screen.getByText("Page 801: ready")).toBeInTheDocument());
  expect(screen.getByText("exact passage")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button"));
  expect(revoke).toHaveBeenCalledWith("blob:original");
  view.unmount(); vi.unstubAllGlobals();
});

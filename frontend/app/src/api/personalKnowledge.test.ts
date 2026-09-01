import { beforeEach, describe, expect, it, vi } from "vitest";
import { deletePersonalKnowledge, getKnowledgeStorageUsage } from "./personalKnowledge";

const clientMocks = vi.hoisted(() => ({
  deleteJSON: vi.fn(),
  getJSON: vi.fn(),
}));

vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  deleteJSON: clientMocks.deleteJSON,
  getJSON: clientMocks.getJSON,
}));

describe("personal knowledge storage API", () => {
  beforeEach(() => {
    clientMocks.deleteJSON.mockReset();
    clientMocks.getJSON.mockReset();
  });

  it("requests the owner-scoped knowledge storage usage endpoint", async () => {
    const usage = {
      used_bytes: 1,
      limit_bytes: 2,
      available_bytes: 1,
      available_document_bytes: 1,
      cleanup_pending_bytes: 0,
      retrying_cleanup_bytes: 0,
      reserved_bytes: 0,
      cleanup_pending_count: 0,
      retrying_cleanup_count: 0,
      reserved_count: 0,
    };
    clientMocks.getJSON.mockResolvedValue(usage);

    await expect(getKnowledgeStorageUsage()).resolves.toBe(usage);
    expect(clientMocks.getJSON).toHaveBeenCalledWith("/knowledge/storage/usage");
  });

  it("accepts the asynchronous deletion response", async () => {
    const response = {
      status: "deletion_pending" as const,
      id: "doc-1",
      cleanup_operation_id: "cleanup-1",
    };
    clientMocks.deleteJSON.mockResolvedValue(response);

    await expect(deletePersonalKnowledge("doc-1")).resolves.toBe(response);
    expect(clientMocks.deleteJSON).toHaveBeenCalledWith("/knowledge/documents/doc-1");
  });
});

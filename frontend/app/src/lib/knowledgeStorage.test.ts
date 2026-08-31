import { describe, expect, it } from "vitest";
import { APIError } from "@/api/client";
import {
  isKnowledgeStorageQuotaExceeded,
  knowledgeStorageQuotaCopy,
} from "./knowledgeStorage";

describe("knowledge storage error copy", () => {
  it("recognizes the stable backend error envelope", () => {
    const error = new APIError(
      413,
      "knowledge_storage_quota_exceeded",
      { error: { code: "knowledge_storage_quota_exceeded" } },
    );

    expect(isKnowledgeStorageQuotaExceeded(error)).toBe(true);
    expect(knowledgeStorageQuotaCopy("zh-CN").description).toContain("知识库空间已满");
  });

  it("does not treat provider quota errors as knowledge storage", () => {
    const error = new APIError(
      429,
      "provider_quota_exceeded",
      { error: { code: "provider_quota_exceeded" } },
    );

    expect(isKnowledgeStorageQuotaExceeded(error)).toBe(false);
  });
});

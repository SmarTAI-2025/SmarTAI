import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import type { Locale } from "@/i18n/messages";

export const KNOWLEDGE_STORAGE_QUOTA_EXCEEDED = "knowledge_storage_quota_exceeded";

export function isKnowledgeStorageQuotaExceeded(error: unknown): boolean {
  const normalized = normalizeAPIError(error);
  return normalized.status === 413
    && getAPIErrorCode(normalized) === KNOWLEDGE_STORAGE_QUOTA_EXCEEDED;
}
export function knowledgeStorageQuotaCopy(locale: Locale): {
  title: string;
  description: string;
} {
  if (locale === "zh-CN") {
    return {
      title: "知识库空间不足",
      description: "知识库空间已满或剩余空间不足。请删除不再需要的资料；若系统正在后台清理，请等待自动完成后再上传。",
    };
  }
  return {
    title: "Not enough knowledge storage",
    description: "Knowledge storage is full or does not have enough room. Delete materials you no longer need; if background cleanup is running, wait for it to finish automatically before uploading.",
  };
}

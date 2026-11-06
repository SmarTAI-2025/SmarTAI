import { getJSON } from "./client";
import type { KnowledgeStorageUsage } from "@/types/personalKnowledge";

export function getKnowledgeStorageUsage(): Promise<KnowledgeStorageUsage> {
  return getJSON("/knowledge/storage/usage");
}

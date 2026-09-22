import { deleteJSON, getJSON, postMultipart, type UploadOptions } from "./client";
import type {
  DeletePersonalKnowledgeResponse,
  KnowledgeStorageUsage,
  PersonalKnowledgeDocument,
  PersonalKnowledgeListResponse,
} from "@/types/personalKnowledge";

export function listPersonalKnowledge(): Promise<PersonalKnowledgeListResponse> {
  return getJSON("/knowledge/documents");
}

export function getKnowledgeStorageUsage(): Promise<KnowledgeStorageUsage> {
  return getJSON("/knowledge/storage/usage");
}

export function uploadPersonalKnowledge(file: File, options?: UploadOptions): Promise<PersonalKnowledgeDocument> {
  return postMultipart("/knowledge/documents", file, options);
}

export function deletePersonalKnowledge(documentId: string): Promise<DeletePersonalKnowledgeResponse> {
  return deleteJSON(`/knowledge/documents/${documentId}`);
}

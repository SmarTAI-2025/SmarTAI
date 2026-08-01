import { deleteJSON, getJSON, postMultipart, type UploadOptions } from "./client";
import type { PersonalKnowledgeDocument, PersonalKnowledgeListResponse } from "@/types/personalKnowledge";

export function listPersonalKnowledge(): Promise<PersonalKnowledgeListResponse> {
  return getJSON("/knowledge/documents");
}

export function uploadPersonalKnowledge(file: File, options?: UploadOptions): Promise<PersonalKnowledgeDocument> {
  return postMultipart("/knowledge/documents", file, options);
}

export function deletePersonalKnowledge(documentId: string): Promise<{ status: string; id: string }> {
  return deleteJSON(`/knowledge/documents/${documentId}`);
}

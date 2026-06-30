import { deleteJSON, getJSON, postMultipart, type UploadOptions } from "./client";
import type { KBDeleteResponse, KBListResponse, KBUploadResponse } from "@/types";

export function uploadKBDoc(taskId: string, file: File, options?: UploadOptions): Promise<KBUploadResponse> {
  return postMultipart<KBUploadResponse>(`/tasks/${taskId}/kb`, file, options);
}

export function listKBDocs(taskId: string): Promise<KBListResponse> {
  return getJSON<KBListResponse>(`/tasks/${taskId}/kb`);
}

export function deleteKBDoc(taskId: string, docId: string): Promise<KBDeleteResponse> {
  return deleteJSON<KBDeleteResponse>(`/tasks/${taskId}/kb/${docId}`);
}

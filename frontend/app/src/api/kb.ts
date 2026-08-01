import { deleteJSON, getJSON, postMultipart, type UploadOptions } from "./client";
import type { KBDeleteResponse, KBListResponse, KBUploadResponse } from "@/types";

export type AddKBDocInput = {
  file?: File;
  libraryMaterialId?: string;
  saveToLibrary?: boolean;
  expectedWorkflowRevision?: number;
  onProgress?: UploadOptions["onProgress"];
};

export function addKBDoc(taskId: string, input: AddKBDocInput): Promise<KBUploadResponse> {
  return postMultipart<KBUploadResponse>(`/tasks/${taskId}/kb`, input.file ?? null, {
    onProgress: input.onProgress,
    fields: {
      library_material_id: input.libraryMaterialId,
      save_to_library: input.saveToLibrary,
      expected_workflow_revision: input.expectedWorkflowRevision,
    },
  });
}

export function listKBDocs(taskId: string): Promise<KBListResponse> {
  return getJSON<KBListResponse>(`/tasks/${taskId}/kb`);
}

export function deleteKBDoc(taskId: string, docId: string, expectedWorkflowRevision?: number): Promise<KBDeleteResponse> {
  return deleteJSON<KBDeleteResponse>(`/tasks/${taskId}/kb/${docId}`, {
    params: expectedWorkflowRevision === undefined
      ? undefined
      : { expected_workflow_revision: expectedWorkflowRevision },
  });
}

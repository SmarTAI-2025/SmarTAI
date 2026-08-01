import { apiClient, getJSON, normalizeAPIError, postJSON } from "./client";
import type {
  ApplyMaterialImportInput,
  ApplyMaterialImportResponse,
  MaterialImportPlanResponse,
  MaterialImportPreflightInput,
  MaterialImportPreflightResponse,
  MaterialImportStartResponse,
} from "@/types";

export async function preflightMaterialImport(
  input: MaterialImportPreflightInput,
): Promise<MaterialImportPreflightResponse> {
  const formData = new FormData();
  if (input.file) formData.append("file", input.file);
  if (input.libraryMaterialId) formData.append("library_material_id", input.libraryMaterialId);
  formData.append("targets", JSON.stringify(input.targets));
  formData.append("structure_mode", input.structureMode);
  formData.append("extraction_hint", input.extractionHint.trim());
  formData.append("save_to_library", String(input.saveToLibrary));

  try {
    const response = await apiClient.post<MaterialImportPreflightResponse>(
      `/tasks/${input.taskId}/material-imports/preflight`,
      formData,
      { timeout: 180_000 },
    );
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export function startMaterialImport(taskId: string, sourceToken: string) {
  return postJSON<MaterialImportStartResponse, { source_token: string }>(
    `/tasks/${taskId}/material-imports`,
    { source_token: sourceToken },
  );
}

export function getMaterialImport(taskId: string, jobId: string) {
  return getJSON<MaterialImportPlanResponse>(`/tasks/${taskId}/material-imports/${jobId}`);
}

export function applyMaterialImport(input: ApplyMaterialImportInput) {
  return postJSON<ApplyMaterialImportResponse, {
    accepted_candidate_ids: string[];
    overwrite_candidate_ids: string[];
    expected_workflow_revision: number;
  }>(`/tasks/${input.taskId}/material-imports/${input.jobId}/apply`, {
    accepted_candidate_ids: input.acceptedCandidateIds,
    overwrite_candidate_ids: input.overwriteCandidateIds,
    expected_workflow_revision: input.expectedWorkflowRevision,
  });
}

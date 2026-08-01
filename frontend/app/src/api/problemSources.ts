import { apiClient, getJSON, normalizeAPIError } from "./client";
import type {
  ProblemLibraryResponse,
  QuestionPreparationCapabilities,
  ProblemSourcePreflightInput,
  ProblemSourcePreflightResponse,
  ProblemSourceScope,
  StartProblemExtractionInput,
  StartProblemExtractionResponse,
  StartQuestionPreparationInput,
  StartQuestionPreparationResponse,
} from "@/types";

export function getQuestionPreparationCapabilities(
  taskId: string,
): Promise<QuestionPreparationCapabilities> {
  return getJSON<QuestionPreparationCapabilities>(
    `/tasks/${taskId}/question-preparation/capabilities`,
  );
}

export function listProblemSourceLibrary(
  taskId: string,
  options: { scope: ProblemSourceScope; query?: string },
): Promise<ProblemLibraryResponse> {
  return getJSON<ProblemLibraryResponse>(`/tasks/${taskId}/problem-sources/library`, {
    params: {
      scope: options.scope,
      q: options.query?.trim() || undefined,
    },
  });
}

export async function preflightProblemSource(
  input: ProblemSourcePreflightInput,
): Promise<ProblemSourcePreflightResponse> {
  const formData = new FormData();
  if (input.mode === "upload" && input.file) {
    formData.append("file", input.file);
  }
  if (input.mode === "library" && input.libraryMaterialId) {
    formData.append("library_material_id", input.libraryMaterialId);
  }
  if (input.mode === "inline_text" && input.inlineText?.trim()) {
    formData.append("inline_text", input.inlineText.trim());
  }
  formData.append("structure_mode", input.structureMode);
  formData.append("role", input.role ?? "problem");
  formData.append("extraction_hint", input.extractionHint?.trim() ?? "");
  formData.append("save_to_library", String(input.saveToLibrary));

  try {
    const response = await apiClient.post<ProblemSourcePreflightResponse>(
      `/tasks/${input.taskId}/question-preparation/sources/preflight`,
      formData,
      { timeout: 180_000 },
    );
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function startQuestionPreparation(
  input: StartQuestionPreparationInput,
): Promise<StartQuestionPreparationResponse> {
  try {
    const response = await apiClient.post<StartQuestionPreparationResponse>(
      `/tasks/${input.taskId}/question-preparation/jobs`,
      {
        source_tokens: input.sourceTokens,
        expected_workflow_revision: input.expectedWorkflowRevision,
        replace_confirmed: input.replaceConfirmed ?? false,
        generation_policy: "complete_required_materials",
      },
      { timeout: 180_000 },
    );
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function startProblemExtraction(
  input: StartProblemExtractionInput,
): Promise<StartProblemExtractionResponse> {
  const formData = new FormData();
  formData.append("source_token", input.sourceToken);
  formData.append("confirmed_candidate_ids", JSON.stringify(input.confirmedCandidateIds ?? []));
  formData.append("replace_confirmed", String(input.replaceConfirmed ?? false));

  try {
    const response = await apiClient.post<StartProblemExtractionResponse>(
      `/tasks/${input.taskId}/extract_problems`,
      formData,
      { timeout: 180_000 },
    );
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

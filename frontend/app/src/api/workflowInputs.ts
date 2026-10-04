import { useQuery } from "@tanstack/react-query";
import { getJSON } from "./client";
import { useDraftOwner } from "@/hooks/useDraftProtection";
import type { PreparationSourceRole, ProblemSourceMode, ProblemStructureMode, QuestionScorePolicyInput, SubmissionIdentityMode } from "@/types";

export interface ProblemInput {
  job_id: string; recognition_provider_id: string;
  score_policy: { mode: QuestionScorePolicyInput["mode"]; uniform_max_score?: number | null; per_question_text?: string | null };
  sources: Array<{
    source_token: string; role: PreparationSourceRole; source_kind: ProblemSourceMode;
    filename: string; stored_file_id: string | null; library_material_id: string | null;
    inline_text: string; structure_mode: ProblemStructureMode; extraction_hint: string;
    recognition_options: { pages?: number[]; targets?: string[] };
    enable_material_ocr: boolean; save_to_library: boolean; available: boolean;
  }>;
}
export interface SubmissionInput {
  job_id: string; stored_file_id: string | null; filename: string | null; available: boolean;
  identity_mode: SubmissionIdentityMode; roster_name: string | null; roster_count: number;
  recognition_provider_id: string;
}
export interface WorkflowInput<T> { task_id: string; workflow_revision: number; input: T | null }

export function useWorkflowInput<S extends "problems" | "submissions">(taskId: string | undefined, stage: S, enabled = true) {
  const owner = useDraftOwner();
  return useQuery({
    queryKey: ["workflow-input", owner, taskId, stage],
    queryFn: () => getJSON<WorkflowInput<S extends "problems" ? ProblemInput : SubmissionInput>>(`/tasks/${encodeURIComponent(taskId!)}/${stage === "problems" ? "question-preparation" : "submission-recognition"}/input`),
    enabled: Boolean(taskId) && enabled, retry: false, refetchOnMount: "always", refetchOnWindowFocus: false,
  });
}

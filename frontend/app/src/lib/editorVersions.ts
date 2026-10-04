import type { AICompletionPreflightResponse, CourseMaterial, GradingSetupResponse, MaterialImportPlanResponse, Task } from "@/types";

function draftFingerprint(value: unknown): string {
  return JSON.stringify(value, (_key, entry: unknown) => entry && typeof entry === "object" && !Array.isArray(entry)
    ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);
}

// Editor conflicts track their business inputs, never the task-wide mutation
// counter, timestamps or polling state. Server mutations still require CAS.
export function taskInputVersion(task: Task | undefined, stage: "problems" | "submissions" | "material-import") {
  if (!task) return "";
  const questions = Object.fromEntries(Object.entries(task.problem_data ?? {}).map(([id, problem]) => [id, {
    q_id: problem.q_id, number: problem.number, type: problem.type, stem: problem.stem,
    ...(stage !== "submissions" ? { criterion: problem.criterion, max_score: problem.max_score,
      reference_answer: problem.reference_answer, solution_code: problem.solution_code,
      test_cases: problem.test_cases, question_structure: problem.question_structure } : {}),
  }]));
  const students = stage === "submissions" ? Object.fromEntries(Object.entries(task.student_data ?? {}).map(([id, student]) => [id, {
    id: student.stu_id, name: student.stu_name, source: student.source_id,
    answers: student.stu_ans.map(answer => ({ q_id: answer.q_id, content: answer.content })).sort((a, b) => a.q_id.localeCompare(b.q_id)),
  }])) : undefined;
  return draftFingerprint({ stage, course: task.course_id, questions,
    ...(stage === "problems" ? { source: task.problem_file_name, job: task.extract_job_id } : {}),
    ...(stage === "submissions" ? { source: task.submission_file_name, job: task.parse_job_id, students } : {}),
  });
}

export function gradingEditorVersion(response: GradingSetupResponse | undefined) {
  if (!response) return "";
  return draftFingerprint({ setup: response.grading_setup ?? response.suggested_setup,
    experts: response.available_experts.map(expert => ({ id: expert.provider_id, model: expert.model, type: expert.provider_type, enabled: expert.enabled, shared: expert.is_shared })).sort((a, b) => a.id.localeCompare(b.id)),
  });
}

export function completionSelectionVersion(preflight: AICompletionPreflightResponse | undefined) {
  return draftFingerprint(preflight?.missing_targets.slice().sort((a, b) => a.target_id.localeCompare(b.target_id)) ?? []);
}

export function materialSelectionVersion(plan: MaterialImportPlanResponse | undefined, task: Task | undefined) {
  const candidates = plan?.candidates.slice().sort((a, b) => a.candidate_id.localeCompare(b.candidate_id));
  return draftFingerprint({ plan: plan?.request_fingerprint, candidates,
    // The same candidate must be reconsidered if its destination changes.
    destinations: candidates?.map(candidate => {
      const problem = task?.problem_data?.[candidate.q_id];
      return [candidate.q_id, candidate.target, problem?.stem, problem?.type, problem?.[candidate.target]];
    }),
  });
}

export function materialMetadataVersion(material: CourseMaterial) {
  return draftFingerprint({ filename: material.filename, course: material.course_id,
    group: material.group_id, category: material.category, labels: material.labels });
}

import { describe, expect, it } from "vitest";
import type { AICompletionPreflightResponse, CourseMaterial, GradingSetupResponse, MaterialImportPlanResponse, Task } from "@/types";
import { completionSelectionVersion, gradingEditorVersion, materialMetadataVersion, materialSelectionVersion, taskInputVersion } from "./editorVersions";

const task = { course_id: "course-1", workflow_revision: 1, problem_data: {
  q1: { q_id: "q1", number: "1", type: "计算题", stem: "2+2", criterion: "4", max_score: 10 },
}, student_data: {}, extract_job_id: "source-1", parse_job_id: "answers-1" } as unknown as Task;

describe("editor conflict boundaries", () => {
  it.each(["problems", "submissions", "material-import"] as const)("%s ignores knowledge, status, timestamps and unrelated revision increments", stage => {
    const updated = { ...task, workflow_revision: 40, updated_at: 456, status: "graded", kb_doc_count: 2, kb_docs: { file: { doc_id: "file", filename: "new notes", chunk_count: 1 } }, progress_percent: 100 } as Task;
    expect(taskInputVersion(updated, stage)).toBe(taskInputVersion(task, stage));
    expect(taskInputVersion({ ...task, course_id: "other" }, stage)).not.toBe(taskInputVersion(task, stage));
    const changed = { ...task, problem_data: { q1: { ...task.problem_data.q1, stem: "2+3" } } };
    expect(taskInputVersion(changed, stage)).not.toBe(taskInputVersion(task, stage));
  });
  it("keeps grading configuration independent of knowledge attachment and readiness polling", () => {
    const setup = { strictness: 50, selected_provider_ids: ["p"] };
    const response = { grading_setup: setup, available_experts: [{ provider_id: "p", enabled: true, model: "m", is_shared: false }] } as unknown as GradingSetupResponse;
    expect(gradingEditorVersion({ ...response, workflow_revision: 10, knowledge: { task_doc_count: 3 } } as GradingSetupResponse)).toBe(gradingEditorVersion(response));
    expect(gradingEditorVersion({ ...response, grading_setup: { ...response.grading_setup!, strictness: 70 } })).not.toBe(gradingEditorVersion(response));
    expect(gradingEditorVersion({ ...response, available_experts: [{ ...response.available_experts[0], enabled: false }] })).not.toBe(gradingEditorVersion(response));
  });
  it("submission input ignores review flags while detecting edited answer content", () => {
    const submission = { ...task, student_data: { s: { stu_id: "s", stu_name: "S", stu_ans: [{ q_id: "q1", number: "1", type: "math", content: "4", flag: [] }] } } };
    const reviewed = structuredClone(submission);
    reviewed.student_data.s.stu_ans[0] = { ...reviewed.student_data.s.stu_ans[0], review_status: "confirmed" } as typeof reviewed.student_data.s.stu_ans[0];
    expect(taskInputVersion(reviewed, "submissions")).toBe(taskInputVersion(submission, "submissions"));
    reviewed.student_data.s.stu_ans[0].content = "5";
    expect(taskInputVersion(reviewed, "submissions")).not.toBe(taskInputVersion(submission, "submissions"));
  });
  it("keeps AI selections on an unrelated mutation but detects changed targets", () => {
    const preflight = { workflow_revision: 1, missing_targets: [{ target_id: "q1:criterion", q_id: "q1", target: "criterion" }] } as AICompletionPreflightResponse;
    expect(completionSelectionVersion({ ...preflight, workflow_revision: 2 })).toBe(completionSelectionVersion(preflight));
    expect(completionSelectionVersion({ ...preflight, missing_targets: [] })).not.toBe(completionSelectionVersion(preflight));
  });
  it("keeps matching selections on polling but detects changed candidates or destination content", () => {
    const plan = { workflow_revision: 1, request_fingerprint: "source-plan", candidates: [{ candidate_id: "c", q_id: "q1", target: "criterion", text_value: "answer" }] } as MaterialImportPlanResponse;
    expect(materialSelectionVersion({ ...plan, workflow_revision: 2 }, task)).toBe(materialSelectionVersion(plan, task));
    const changed = { ...task, problem_data: { q1: { ...task.problem_data.q1, criterion: "teacher edit" } } };
    expect(materialSelectionVersion(plan, changed)).not.toBe(materialSelectionVersion(plan, task));
    expect(materialSelectionVersion({ ...plan, candidates: [] }, task)).not.toBe(materialSelectionVersion(plan, task));
  });
  it("library metadata ignores reference counts and parsing updates but detects another metadata edit", () => {
    const material = { filename: "notes.pdf", course_id: "c", group_id: "g", category: "lecture", labels: ["math"] } as CourseMaterial;
    expect(materialMetadataVersion({ ...material, task_reference_count: 4, updated_at: 999, parse_status: "ready" })).toBe(materialMetadataVersion(material));
    expect(materialMetadataVersion({ ...material, filename: "renamed.pdf" })).not.toBe(materialMetadataVersion(material));
  });
});

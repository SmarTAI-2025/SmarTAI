import { describe, expect, it } from "vitest";
import { answerReviewBlocked, answerReviewNextBlocked, questionReviewBlocked, questionReviewNextBlocked, questionReviewIssues, questionReviewBlockers, questionReviewConfirmed, questionIssueNeedsReview, recognitionIssueLabel, submissionReviewBlockers, submissionConfirmationBlockers, studentIdentityReviewBlocked } from "./reviewConfirmation";
import type { Task, ProblemInfo, StudentAnswerInfo, PreparationIssue, StudentSubmission } from "@/types";
import { questionReviewNotes } from "./reviewConfirmation";

const question = (overrides: Partial<ProblemInfo> = {}): ProblemInfo => ({ q_id: "q1", number: "1", type: "concept", stem: "Question text", criterion: "Scoring rubric", reference_answer: "Reference answer", max_score: 10, ...overrides });
const issue = (overrides: Partial<PreparationIssue> = {}): PreparationIssue => ({ issue_id: "i1", q_id: "q1", field: "stem", code: "recognition_needs_review", severity: "warning", status: "open", ...overrides });

describe("review hard failures", () => {
  it.each(["zh-CN", "en-US"])("lists invalid identity fields individually and omits valid fields (%s)", locale => {
    const student = { stu_id: "[Unknown Student]", stu_name: "[Unknown Student]", stu_ans: [] };
    const task = { task_id: "T1", student_data: { S1: student } } as unknown as Task;
    const blockers = submissionReviewBlockers(task, locale);
    expect(blockers).toHaveLength(2);
    expect(blockers[0].label).toMatch(/学生姓名|student name/);
    expect(blockers[1].label).toMatch(/学号|student ID/);
    student.stu_id = "S1";
    const remaining = submissionReviewBlockers(task, locale);
    expect(remaining).toHaveLength(1);
    expect(remaining[0].label).not.toMatch(/学号|student ID/);
  });

  it.each(["zh-CN", "en-US"])("lists each invalid recognition field on its own line (%s)", locale => {
    const answer = { q_id: "q1", number: "1", type: "", content: "Verified transcription",
      flag: ["recognition_invalid_fields:number,type", "recognition_metadata_review", "recognition_failed", "recognition_manual_pending", "authorship_uncertain"] };
    const task = { task_id: "T1", student_data: { S1: { stu_id: "S1", stu_name: "Sample", stu_ans: [answer] } } } as unknown as Task;
    const blockers = submissionReviewBlockers(task, locale);
    expect(blockers).toHaveLength(2);
    expect(blockers[0].label).toMatch(/题号|question number/);
    expect(blockers[0].label).not.toMatch(/题型|question type/);
    expect(blockers[1].label).toMatch(/题型|question type/);
    expect(blockers[1].label).not.toMatch(/题号|question number/);
  });

  it.each(["zh-CN", "en-US"])("lists each missing question field separately and excludes every optional note (%s)", locale => {
    const row = question({ number: "", type: "", stem: "", criterion: "", reference_answer: "",
      preparation_issues: [issue({ code: "low_confidence", details: { confidence: .2 } })] });
    const blockers = questionReviewBlockers([row], "T1", locale);
    expect(blockers).toHaveLength(4);
    const fields = locale === "zh-CN" ? ["题号", "题型", "题干", "评分标准"] : ["question number", "question type", "question text", "rubric"];
    blockers.forEach((blocker, index) => {
      expect(blocker.label).toContain(fields[index]);
      expect(blocker.label).not.toMatch(/标答|reference answer|置信度|confidence|确认|confirm/i);
      expect(blocker.href).toContain("/questions/q1/content#question-q1");
    });
  });

  it.each(["zh-CN", "en-US"])("lists only blocking answer notes as separate items (%s)", locale => {
    const answer = { q_id: "q1", number: "1", type: "concept", content: "", recognition_confidence: .2,
      flag: ["answer_assignment_conflict", "parse_failed", "authorship_uncertain"] };
    const task = { task_id: "T1", student_data: { S1: { stu_id: "S1", stu_name: "Sample", stu_ans: [answer] } } } as unknown as Task;
    const blockers = submissionReviewBlockers(task, locale);
    expect(blockers).toHaveLength(2);
    expect(blockers[0].label).toMatch(/同一题号|Multiple answers/);
    expect(blockers[1].label).toMatch(/解析失败|parsing failed/);
    for (const blocker of blockers) expect(blocker.label).not.toMatch(/置信度|confidence|笔迹|writing|确认|confirm/i);
    answer.flag = ["authorship_uncertain"];
    answer.content = "Saved transcription";
    expect(submissionReviewBlockers(task, locale)).toEqual([]);
  });
  it.each(["zh-CN", "en-US"])("blocks only current missing fields, then releases saved repairs without confirmation (%s)", locale => {
    const row = question({ type: "", reference_answer: "", review_status: "needs_review", preparation_issues: [
      issue({ code: "manual_completion_required", field: "source", details: { required_fields: ["stem", "criterion"] } }),
    ] });
    const blockers = questionReviewBlockers([row], "T1", locale);
    expect(blockers).toHaveLength(1);
    expect(blockers[0].label).toContain(locale === "zh-CN" ? "题型" : "question type");
    expect(blockers[0].label).not.toMatch(/标答|reference answer|已修正|confirm/i);
    const saved = { ...row, type: "concept", review_status: "edited" as const };
    expect(questionReviewBlockers([saved], "T1", locale)).toEqual([]);
    expect(questionReviewIssues(saved).filter(issue => issue.severity === "blocking")).toEqual([]);
  });

  it.each(["zh-CN", "en-US"])("keeps optional answers out of required prompts and clears repaired fields (%s)", locale => {
    const required = issue({ code: "generation_failed", field: "rubric", severity: "blocking" });
    const optional = issue({ issue_id: "answer", code: "generation_failed", field: "answer" });
    const row = question({ criterion: "", reference_answer: "", review_status: "needs_review", preparation_issues: [required, optional] });
    const notes = questionReviewNotes(row, locale, () => "Generation failed");
    const answerName = locale === "zh-CN" ? "标答" : "reference answer";
    expect(notes.filter(note => note.severity === "blocking").every(note => !note.label.includes(answerName))).toBe(true);
    expect(notes.some(note => note.severity === "advisory" && note.label.includes(answerName))).toBe(true);
    expect(questionReviewBlocked(row)).toBe(true);
    const repaired = { ...row, criterion: "Full credit for a correct answer.", preparation_issues: [{ ...required, status: "resolved" as const }, optional] };
    expect(questionReviewBlocked(repaired)).toBe(false);
    expect(questionReviewNextBlocked(repaired)).toBe(false);
    expect(questionReviewNotes(repaired, locale, () => "Generation failed").every(note => note.severity === "advisory")).toBe(true);
    expect(questionReviewNotes(repaired, locale, () => "Generation failed").some(note => /评分标准|rubric/.test(note.label))).toBe(false);
    expect(questionReviewNotes({ ...repaired, review_status: "confirmed", preparation_issues: [{ ...required, status: "resolved" }, { ...optional, status: "acknowledged" }] }, locale, () => "Generation failed")).toEqual([]);
  });

  it.each(["", " ", "unknown"])("requires a real question type without discarding the stem (%s)", type => {
    const row = question({ type });
    expect(questionReviewBlocked(row)).toBe(true);
    expect(questionReviewNextBlocked(row)).toBe(true);
    expect(questionReviewNotes(row, "zh-CN", code => code)).toContainEqual({ label: "请补齐缺失的题型。", severity: "blocking" });
    expect(questionReviewBlocked({ ...row, type: "other" })).toBe(false);
  });

  it("lists current missing fields and deduplicates failure labels at the strongest severity", () => {
    const row = question({ type: "", criterion: "", reference_answer: "", preparation_issues: [
      issue({ code: "generation_failed", field: "rubric", severity: "blocking" }),
      issue({ issue_id: "answer", code: "generation_failed", field: "answer" }),
      issue({ issue_id: "manual", code: "manual_completion_required", field: "source", details: { required_fields: ["criterion"] } }),
    ] });
    const notes = questionReviewNotes(row, "zh-CN", () => "生成失败");
    expect(notes).toEqual([
      { label: "生成失败", severity: "blocking" },
      { label: "请补齐缺失的题型、评分标准。", severity: "blocking" },
      { label: "缺少标答（可选），可按需补充。", severity: "advisory" },
    ]);
    expect(questionReviewNotes(row, "en-US", () => "Generation failed")[1].label).toBe("Complete the missing question type, rubric.");
    expect(questionReviewNotes({ ...row, type: "reasoning" }, "zh-CN", () => "生成失败")[1].label).not.toContain("题型");
  });

  it("keeps independent advisory warnings and does not make a reference answer mandatory", () => {
    const row = question({ reference_answer: "", preparation_issues: [issue({ code: "math_rubric_advisory", field: "rubric" })] });
    expect(questionReviewNextBlocked(row)).toBe(false);
    const notes = questionReviewNotes(row, "zh-CN", code => code);
    expect(notes).toHaveLength(2);
    expect(notes.every(note => note.severity === "advisory")).toBe(true);
    expect(notes[1].label).toBe("缺少标答（可选），可按需补充。");
  });

  it("releases repaired required fields even when the optional answer is missing", () => {
    const row = question({ reference_answer: "", preparation_issues: [issue({ code: "manual_completion_required", field: "source", details: { required_fields: ["type", "criterion"] } })] });
    expect(questionReviewNotes(row, "zh-CN", code => code)).toEqual([{ label: "缺少标答（可选），可按需补充。", severity: "advisory" }]);
    expect(questionReviewBlocked(row)).toBe(false);
    expect(questionReviewNextBlocked(row)).toBe(false);
  });
  it("explains conflicting test expectations and requires confirmation before proceeding", () => {
    const marker = issue({ code: "source_conflict", field: "programming_tests", details: { reason: "test_case_output_conflict" } });
    expect(recognitionIssueLabel(marker, "zh-CN")).toContain("同一输入对应了不同预期输出");
    expect(recognitionIssueLabel(marker, "en-US")).toContain("different expected outputs for the same input");
    expect(questionReviewNextBlocked(question({ preparation_issues: [marker] }))).toBe(true);
  });

  it("shows a short localized rubric hint without reopening the confirmation gate", () => {
    const warning = issue({
      code: "math_rubric_advisory", field: "rubric",
      details: { message: "本轮 AI 评分解释：原有明确规则优先；后续错误不重复扣分。" },
    });
    const problem = question({
      review_status: "confirmed", max_score_review_status: "confirmed",
      preparation_issues: [warning],
    });
    expect(questionReviewIssues(problem)).toEqual([warning]);
    expect(recognitionIssueLabel(warning, "zh-CN")).toBe("本题评分标准有待核对的内容。");
    expect(recognitionIssueLabel(warning, "en-US")).toBe("Some rubric details need review.");
    expect(warning.details?.message).toContain("本轮 AI 评分解释");
    expect(questionReviewConfirmed(problem)).toBe(true);
    expect(questionReviewBlocked(problem)).toBe(false);
    expect(questionReviewNextBlocked(problem)).toBe(false);
    expect(questionReviewBlockers([problem], "T1", "zh-CN")).toEqual([]);
  });

  it("allows a genuine blank response but blocks failed recognition", () => {
    const blank = {q_id:"q1", number:"1", type:"concept", content:"", flag:[]} as StudentAnswerInfo;
    expect(answerReviewBlocked(blank)).toBe(false);
    expect(answerReviewBlocked({...blank, flag:["recognition_failed"]})).toBe(true);
  });
  it("allows uncertain content and blocks an empty question", () => {
    expect(questionReviewBlocked({number:"1", type:"concept", stem:"content", criterion:"Scoring rubric", preparation_issues:[{status:"open", severity:"warning"}]} as ProblemInfo)).toBe(false);
    expect(questionReviewBlocked({stem:""} as ProblemInfo)).toBe(true);
  });
  it("routes a source with no student record to recovery and a failed answer to its exact question", () => {
    const task = {task_id:"T1", submission_sources:[{source_id:"source-1", file_name:"sample.pdf", status:"failed"}], student_data:{internal:{stu_id:"S1", stu_name:"Sample", stu_ans:[{q_id:"q1", content:"", flag:["recognition_failed"]}]}}} as unknown as Task;
    const issues=submissionReviewBlockers(task,"en-US");
    expect(issues).toHaveLength(2);
    expect(issues[0].href).toContain("/submissions/upload");
    expect(issues[1].href).toBe("/tasks/T1/students/S1?question=q1");
  });

  it("keeps source-wide uncertainty and ordinary confirmation out of the question risk queue", () => {
    const warnings = [issue(), issue({ code: "recognition_partial", details: { coverage: { unverified_targets: ["1", "2"] } } }),
      issue({ details: { confidence_reasons: ["quality_uncalibrated"] } })];
    const problems = warnings.map((warning, index) => question({ q_id: `q${index}`, review_status: "needs_review", preparation_issues: [warning] }));
    expect(warnings.every(warning => !questionIssueNeedsReview(warning))).toBe(true);
    expect(problems.flatMap(questionReviewIssues)).toEqual([]);
    expect(questionReviewBlockers(problems, "T1", "zh-CN")).toEqual([]);
    expect(problems[0].preparation_issues).toEqual([warnings[0]]);
  });

  it.each([0, 0.2499, 0.25, 0.5999, 0.60, 1, undefined, NaN, -1])("puts all numeric confidence below .6 into suggestions without blocking (%s)", (confidence) => {
    const warning = issue({ code: "low_confidence", details: { confidence } });
    const problem = question({ preparation_issues: [warning] });
    const suggested = typeof confidence === "number" && confidence >= 0 && confidence < 0.6;
    expect(questionReviewBlocked(problem)).toBe(false);
    expect(questionReviewNextBlocked(problem)).toBe(false);
    expect(questionReviewIssues(problem)).toHaveLength(suggested ? 1 : 0);
    expect(questionReviewBlockers([problem], "T1", "zh-CN")).toHaveLength(0);
    expect(questionReviewNextBlocked({ ...problem, review_status: "confirmed" })).toBe(false);
    if (suggested) expect(questionReviewIssues(problem)[0]).toMatchObject({ severity: "warning" });
  });

  it("uses actual material confidence and keeps the confirm action possible", () => {
    const imported = question({ preparation_issues: [issue({ code: "low_confidence", field: "rubric" })], material_provenance: { criterion: { confidence: 0.2 } as NonNullable<ProblemInfo["material_provenance"]>["criterion"] } });
    expect(questionReviewBlocked(imported)).toBe(false);
    expect(questionReviewNextBlocked(imported)).toBe(false);
    expect(questionReviewIssues(imported)[0].details?.confidence).toBe(0.2);
  });

  it("does not convert categorical file-level low into a numeric mandatory gate", () => {
    const warning = issue({ details: { confidence_reasons: ["quality_uncalibrated", "low_confidence_content"] } });
    const problem = question({ preparation_issues: [warning] });
    expect(questionIssueNeedsReview(warning)).toBe(false);
    expect(questionReviewIssues(problem)).toEqual([]);
    expect(questionReviewNextBlocked(problem)).toBe(false);
  });

  it("does not revive a resolved confidence suggestion as a blocker", () => {
    const warning = issue({ code: "low_confidence", status: "resolved", details: { confidence: 0.2 } });
    const edited = question({ review_status: "edited", preparation_issues: [warning] });
    expect(questionReviewBlocked(edited)).toBe(false);
    expect(questionReviewNextBlocked(edited)).toBe(false);
    expect(questionReviewIssues(edited)).toEqual([]);
    expect(questionReviewNextBlocked({ ...edited, review_status: "confirmed" })).toBe(false);
    expect(questionReviewIssues({ ...edited, review_status: "confirmed" })).toEqual([]);
  });

  it.each(["failed_pages", "unprocessed_pages", "missing_targets"])("requires repair of concrete question coverage failure: %s", (key) => {
    const warning = issue({ code: "recognition_partial", details: { coverage: { [key]: [2] } } });
    const problem = question({ review_status: "confirmed", preparation_issues: [warning] });
    expect(questionIssueNeedsReview(warning)).toBe(true);
    expect(questionReviewBlocked(problem)).toBe(true);
    expect(questionReviewNextBlocked(problem)).toBe(true);
    expect(questionReviewIssues(problem)[0]).toMatchObject({ severity: "blocking" });
    expect(warning.severity).toBe("warning");
  });

  it("surfaces empty question text and conflicting subpart points even without server issues", () => {
    const empty = question({ stem: " " });
    const conflict = question({ rubric_point_summary: { contract_version: 1, has_explicit_subpart_points: true, items: [], major_max_score: "10", total_points: "9", is_valid: false, issue_code: "rubric_subpart_points_mismatch" } });
    expect(questionReviewIssues(empty)[0]).toMatchObject({ severity: "blocking", field: "stem" });
    expect(questionReviewIssues(conflict)[0]).toMatchObject({ severity: "blocking", field: "rubric" });
    expect(questionReviewBlocked(conflict)).toBe(true);
    expect(questionReviewBlockers([empty, conflict], "T1", "en-US")).toHaveLength(2);
  });

  it("requires a usable rubric but only suggests completing a reference answer", () => {
    const missingRubric = question({ criterion: "", reference_answer: "" });
    expect(questionReviewIssues(missingRubric).map(issue => [issue.field, issue.severity])).toEqual([["rubric", "blocking"], ["answer", "warning"]]);
    expect(questionReviewNextBlocked(missingRubric)).toBe(true);
    const missingAnswer = question({ reference_answer: "", preparation_issues: [issue({ code: "manual_completion_required", severity: "blocking", details: { required_fields: ["answer"] } })] });
    expect(questionReviewNextBlocked(missingAnswer)).toBe(false);
    expect(questionReviewIssues(missingAnswer).every(issue => issue.severity === "warning")).toBe(true);
    expect(questionReviewIssues({ ...missingAnswer, review_status: "confirmed" })).toEqual([]);
  });

  it("does not revive resolved failures when a repaired question has not been confirmed", () => {
    const repaired = question({ review_status: "edited", preparation_issues: [issue({ code: "generation_failed", severity: "blocking", status: "resolved" })] });
    expect(questionReviewBlocked(repaired)).toBe(false);
    expect(questionReviewNextBlocked(repaired)).toBe(false);
    expect(questionReviewIssues(repaired)).toEqual([]);
  });

  it.each(["manual_completion_required", "source_conflict", "ambiguous_question_match"] as const)("allows explicit confirmation of usable content with a %s marker", code => {
    const marker = issue({ code, severity: "warning", details: { required_fields: ["criterion"] } });
    const ready = question({ preparation_issues: [marker] });
    expect(questionReviewBlocked(ready)).toBe(false);
    expect(questionReviewNextBlocked(ready)).toBe(code !== "manual_completion_required");
    expect(questionReviewBlocked({ ...ready, criterion: "" })).toBe(true);
    expect(questionReviewNextBlocked({ ...ready, review_status: "confirmed", preparation_issues: [{ ...marker, status: "acknowledged" }] })).toBe(false);
  });

  it("allows a saved repaired answer to continue without confirmation", () => {
    const repaired = { q_id: "q1", number: "1", type: "concept", content: "corrected", flag: [], review_status: "pending" as const };
    expect(answerReviewBlocked(repaired)).toBe(false);
    expect(answerReviewNextBlocked(repaired)).toBe(false);
    const task = { task_id: "T1", student_data: { s1: { stu_id: "s1", stu_name: "Sample", stu_ans: [repaired] } } } as unknown as Task;
    expect(submissionReviewBlockers(task, "en-US")).toHaveLength(0);
    expect(submissionConfirmationBlockers(task, "en-US")).toHaveLength(0);
    expect(answerReviewNextBlocked({ ...repaired, review_status: "confirmed" })).toBe(false);
  });

  it("keeps the legacy very-low-confidence marker optional", () => {
    const answer = { q_id: "q1", number: "1", type: "concept", content: "Visible work", flag: ["recognition_very_low_confidence"] };
    expect(answerReviewBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked({ ...answer, review_status: "confirmed" })).toBe(false);
  });

  it.each(["very_low_confidence", "recognition_low_confidence", "low_confidence"])("does not turn uncalibrated %s into a mandatory review", (flag) => {
    const answer = { q_id: "q1", number: "1", type: "concept", content: "original text", flag: [flag] };
    const task = { task_id: "T1", student_data: { s1: { stu_id: "s1", stu_name: "Sample", stu_ans: [answer] } } } as unknown as Task;
    expect(answerReviewBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked(answer)).toBe(false);
    expect(submissionConfirmationBlockers(task, "en-US")).toEqual([]);
    expect(submissionReviewBlockers(task, "en-US")).toEqual([]);
  });

  it("blocks source-backed identity problems and placeholders, but not legacy optional confirmation", () => {
    const student: StudentSubmission = { stu_id: "s1", stu_name: "Sample", stu_ans: [], identity_status: "needs_review" };
    expect(studentIdentityReviewBlocked(student)).toBe(false);
    expect(studentIdentityReviewBlocked({ ...student, source_id: "source-1" })).toBe(true);
    expect(studentIdentityReviewBlocked({ ...student, source_id: "source-1", identity_status: "matched" })).toBe(false);
    expect(studentIdentityReviewBlocked({ ...student, stu_name: "[Unknown Student]", identity_status: "matched" })).toBe(true);
    expect(studentIdentityReviewBlocked({ ...student, stu_id: "[Unknown Student]", identity_status: "matched" })).toBe(true);
    for (const value of ["Unknown Student", "unknown student", "[unknown student]", "未知学生"]) {
      expect(studentIdentityReviewBlocked({ ...student, stu_id: value, stu_name: value, identity_status: "matched" })).toBe(false);
    }
    const task = { task_id: "T1", student_data: { s1: { ...student, stu_name: "[Unknown Student]" } } } as unknown as Task;
    expect(submissionConfirmationBlockers(task, "en-US")).toHaveLength(1);
  });

  it("keeps an empty manual review in the matrix so student rows can be created", () => {
    const task = { task_id: "T1", submission_sources: [{ source_id: "source-1", file_name: "sample.pdf", status: "identity_needs_review", internal_status: "identity_conflict" }], student_data: {} } as unknown as Task;
    expect(submissionReviewBlockers(task, "zh-CN")[0].href).toBe("/tasks/T1/submissions#student-count");
    expect(submissionConfirmationBlockers(task, "zh-CN")).toHaveLength(1);
  });
});


describe("incremental review contract", () => {
  it.each(["stem", "rubric", "answer"] as const)("preserves projected incomplete %s evidence without page metadata", field => {
    const partial = issue({ code: "recognition_partial", field, severity: field === "answer" ? "warning" : "blocking",
      source_ids: ["source-1"], details: { incomplete: true } });
    const problem = question({ preparation_issues: [partial] });
    expect(questionReviewIssues(problem)).toHaveLength(1);
    expect(questionReviewBlocked(problem)).toBe(field !== "answer");
    expect(questionReviewNextBlocked(problem)).toBe(field !== "answer");
    expect(recognitionIssueLabel(partial, "zh-CN")).toContain("来源内容不完整");
    expect(recognitionIssueLabel(partial, "en-US")).toContain("Source content is incomplete");
    expect(questionReviewIssues({ ...problem, preparation_issues: [{ ...partial, status: "resolved" }] })).toEqual([]);
  });

  it("keeps an explicit blocking fragment visible before enriched details arrive", () => {
    const partial = issue({ code: "recognition_partial", severity: "blocking", details: {} });
    expect(questionReviewBlocked(question({ preparation_issues: [partial] }))).toBe(true);
  });

  it("permits metadata confirmation while unresolved metadata blocks continuation", () => {
    const answer = { q_id: "q1", number: "1", type: "", content: "Verified answer", flag: [
      "recognition_invalid_fields:type", "recognition_manual_pending", "recognition_failed", "recognition_metadata_review"] };
    expect(answerReviewBlocked(answer)).toBe(false);
    expect(answerReviewNextBlocked(answer)).toBe(true);
    expect(answerReviewBlocked({ ...answer, flag: answer.flag.filter(flag => flag !== "recognition_metadata_review") })).toBe(true);
    expect(answerReviewBlocked({ ...answer, content: "" })).toBe(true);
    for (const flag of ["recognition_failed_pages:2", "recognition_unprocessed_pages:3", "recognition_incomplete", "parse_failed", "provider_submit_uncertain", "recognition_invalid_fields:content"]) {
      expect(answerReviewBlocked({ ...answer, flag: [...answer.flag, flag] })).toBe(true);
    }
  });
});

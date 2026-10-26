import { normalizeFilterType } from "./taskFilterIntent";

export const QUESTION_TYPES = ["calculation", "concept", "reasoning", "programming", "other"];

export function hasQuestionType(value: string | undefined): boolean {
  return QUESTION_TYPES.includes(normalizeFilterType(value ?? ""));
}

/** Missing or unrecognized types require an explicit teacher selection. */
export function questionTypeLabel(value: string | undefined, locale: string): string {
  const labels: Record<string, [string, string]> = {
    calculation: ["计算题", "Calculation"], programming: ["编程题", "Programming"],
    reasoning: ["推理题", "Reasoning"], concept: ["概念题", "Concept"],
    other: ["其他", "Other"],
  };
  const label = labels[normalizeFilterType(value ?? "")];
  return label ? label[locale === "zh-CN" ? 0 : 1] : (locale === "zh-CN" ? "请选择" : "Please select");
}

export function testCaseLabel(title: string | undefined, index: number, locale: string): string {
  const standard = title?.match(/^(?:样例|Example)\s*(\d+)$/i);
  return title && !standard ? title : `${locale === "zh-CN" ? "样例" : "Example"} ${standard?.[1] ?? index + 1}`;
}

export function formatSubmissionFlag(flag: string, locale: string): string {
  if (flag.startsWith("recognition_invalid_fields:")) {
    const fields: Record<string, [string, string]> = {
      number: ["题号", "question number"], type: ["题型", "question type"],
      content: ["作答内容", "answer content"], flag: ["识别提示", "recognition notes"],
      recognition_confidence: ["识别置信度", "recognition confidence"],
    };
    const names = [...new Set(flag.slice("recognition_invalid_fields:".length).split(","))]
      .flatMap(field => fields[field] ? [fields[field][locale === "zh-CN" ? 0 : 1]] : []);
    return locale === "zh-CN" ? `待核对：${names.join("、") || "部分识别信息"}`
      : `Review needed: ${names.join(", ") || "some recognition details"}`;
  }
  const labels: Record<string, [string, string]> = {
    answer_assignment_conflict: ["同一题号有多份作答，请更正题号", "Multiple answers use this question; correct the assignment"],
    recognition_matching_review: ["请对照原件核对作答归属与原文，修正后保存", "Check the question assignment and transcription against the original, then save corrections"],
    recognition_needs_review: ["识别结果需对照原件核对", "Check transcription against the original"],
    external_annotation_present: ["原件含外部批注，不属于学生作答", "External annotations are not student answers"],
    authorship_uncertain: ["部分笔迹归属不明，需核对", "Some writing has uncertain authorship"],
    ocr_only_structure_requires_teacher_review: ["题号匹配需教师核对", "Question matching needs teacher review"],
    recognition_failed: ["识别失败，请修正", "Recognition failed; correct"],
    recognition_manual_pending: ["等待人工补齐", "Awaiting manual completion"],
    recognition_metadata_review: ["识别元信息待核对确认", "Confirm the recognition metadata"],
    recognition_paused: ["已暂停，尚未完成识别", "Paused before recognition completed"],
    recognition_incomplete: ["尚未完成识别", "Recognition incomplete"],
    manual_blank_confirmed: ["教师已确认未作答", "Teacher confirmed no answer"],
    parse_failed: ["作答解析失败，请修正", "Answer parsing failed; correct"],
    no_matching_answer: ["未识别到对应作答，请对照原件修正", "No matching answer recognized; correct against the source"],
  };
  const label = labels[flag];
  return label ? label[locale === "zh-CN" ? 0 : 1] : flag;
}

export function questionIssueCodeLabel(code: string, locale: string) {
  const labels: Record<string, [string, string]> = {
    low_confidence: ["匹配置信度较低，请与原文件核对", "Low-confidence match; compare with the source"],
    source_conflict: ["教师资料内容存在冲突，请核对", "Teacher material contains conflicting content; please review"],
    ai_source_conflict: ["SmarTAI 结果与原文件存在冲突", "SmarTAI output conflicts with the source"],
    ambiguous_question_match: ["无法唯一匹配到题号", "Question match is ambiguous"],
    unmapped_source_content: ["原文件中有内容尚未匹配", "Some source content is unmatched"],
    parse_anomaly: ["文件解析结果异常", "File parsing anomaly"],
    generation_failed: ["所需内容生成失败", "Required content generation failed"],
    manual_completion_required: ["请补齐缺失资料", "Complete the missing materials"],
    rubric_step_reference_conflict: ["评分步骤与标答步骤未正确对应", "Rubric steps do not align with reference-answer steps"],
    math_rubric_advisory: ["本题含 AI 补充评分说明，建议核对评分标准", "AI scoring guidance was added. Review the rubric"],
    invalid_test_case: ["测试样例结构无效", "Invalid test case structure"],
    reference_solution_failed_case: ["参考解未通过测试样例", "Reference solution failed a test"],
    default_max_score_requires_review: ["当前使用默认 10 分，请确认题目满分", "Default 10-point maximum; confirm the score"],
    max_score_not_found: ["未从每题分值说明中匹配到本题，已暂按 10 分", "No score matched this question; temporarily set to 10"],
    recognition_partial: ["识别覆盖或内容尚待核对，请对照原文", "Recognition coverage or content needs review against the source"],
    recognition_needs_review: ["识别结果存在不确定内容，请对照原文", "Uncertain recognition content; compare with the source"],
  };
  const label = labels[code];
  return label ? label[locale === "zh-CN" ? 0 : 1]
    : (locale === "zh-CN" ? "资料存在待核对项，请打开详情" : "Material needs review; open the details");
}

import type { StudentAnswerInfo } from "@/types";
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

export const unclearAnswerLabel = (locale: string, confidence?: number) => locale === "zh-CN"
  ? `部分作答识别不清${confidence === undefined ? "" : `（置信度 ${confidence}）`}，建议对照原文件核对。`
  : `Some of the answer was unclear${confidence === undefined ? "" : ` (confidence ${confidence})`}. Check it against the original.`;

export function isUnclearAnswerFlag(flag: string): boolean {
  return ["recognition_unclear", "recognition_unreadable", "unclear_text", "unreadable_text", "unreadable",
    "unreadable_text_present", "recognition_uncertain", "unreadable_content"]
    .includes(flag.trim().toLowerCase().replace(/^recongnition_/, "recognition_"));
}

export const genericAnswerReviewLabel = (locale: string) => locale === "zh-CN"
  ? "建议对照原文件核对作答。" : "Check the answer against the original.";

export function formatSubmissionFlag(flag: string, locale: string): string {
  if (isUnclearAnswerFlag(flag)) return unclearAnswerLabel(locale);
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
  if (flag === "recognition_matching_advisory") flag = "recognition_matching_review";
  if (flag === "question_identifier_mismatch" || flag === "identifier_mismatch") flag = "question_identifier_conflict";
  if (flag === "unclear_crossed_out_work") flag = "crossed_out_text_unclear";
  const labels: Record<string, [string, string]> = {
    answer_assignment_conflict: ["同一学生的该题有多份作答，请更正题号。", "This student has multiple answers assigned to this question. Correct the assignments."],
    recognition_matching_review: ["请对照学生作答原文件，确认该题对应的题号。", "Check the student's original submission and confirm the corresponding question number."],
    question_identifier_conflict: ["请对照学生作答原文件，确认该题对应的题号。", "Check the student's original submission and confirm the corresponding question number."],
    crossed_out_text_unclear: ["部分划掉的文字识别不清，建议对照原文件核对。", "Some crossed-out text could not be read clearly. Check it against the original."],
    recognition_boundary_previous_page_failed: ["作答识别可能不完整，请核对识别失败的上一页是否有遗漏。", "This answer may be incomplete. Check the previous page, which could not be read, for missing work."],
    recognition_boundary_next_page_failed: ["作答识别可能不完整，请核对识别失败的下一页是否有遗漏。", "This answer may be incomplete. Check the next page, which could not be read, for missing work."],
    recognition_boundary_uncertain: ["作答可能未识别完整，请对照原文件核对。", "This answer may be incomplete. Check it against the original."],
    recognition_needs_review: ["识别结果需对照原件核对", "Check transcription against the original"],
    external_annotation_present: ["原件含外部批注，不属于学生作答。", "External annotations are not student answers."],
    authorship_uncertain: ["部分笔迹归属不明，建议对照原文件核对。", "The author of some writing is unclear. Check the original."],
    ocr_only_structure_requires_teacher_review: ["题号匹配需教师核对", "Question matching needs teacher review"],
    recognition_failed: ["识别失败，请修正", "Recognition failed; correct"],
    recognition_manual_pending: ["等待人工补齐", "Awaiting manual completion"],
    recognition_metadata_review: ["识别元信息待核对确认", "Confirm the recognition metadata"],
    recognition_paused: ["作答识别已暂停，可继续识别或人工补齐。", "Answer recognition is paused. Resume recognition or complete it manually."],
    recognition_incomplete: ["作答识别不完整，请对照原文件补齐。", "The answer transcription is incomplete. Check the original and complete it."],
    manual_blank_confirmed: ["教师已确认未作答", "Teacher confirmed no answer"],
    parse_failed: ["作答解析未完成，请重试或对照原文件补齐。", "Answer parsing did not complete. Retry or complete the answer using the original."],
    no_matching_answer: ["未找到与本题对应的作答，请核对原文件和题号。", "No answer was matched to this question. Check the original and the question number."],
  };
  const key = flag.trim().toLowerCase();
  const label = Object.hasOwn(labels, key) ? labels[key] : undefined;
  return label?.[locale === "zh-CN" ? 0 : 1] ?? genericAnswerReviewLabel(locale);
}

export function incompleteSubmissionLabel(answer: StudentAnswerInfo, locale: string, compact: boolean): string {
  const zh = locale === "zh-CN";
  const pageReasons = ["failed", "unprocessed"].flatMap(kind => {
    const prefix = `recognition_${kind}_pages:`;
    const pages = [...new Set((answer.flag ?? []).filter(flag => flag.startsWith(prefix))
      .flatMap(flag => flag.slice(prefix.length).split(",")).filter(page => /^\d+$/.test(page)))];
    if (!pages.length) return [];
    if (zh) return [`第 ${pages.join("、")} 页${kind === "failed" ? "识别失败" : "尚未识别"}`];
    const description = kind === "failed" ? "Recognition failed on" : "Recognition did not reach";
    return [`${description} ${pages.length === 1 ? "page" : "pages"} ${pages.join(", ")}`];
  });
  const failure = pageReasons.join(zh ? "，" : ". ");
  const issue = answer.content?.trim()
    ? (zh ? "作答识别不完整" : "Answer transcription is incomplete")
    : (zh ? "作答内容缺失" : "Answer content is missing");
  if (compact) return zh ? `${failure ? `${failure}，` : ""}${issue}。`
    : `${failure ? `${failure}. ` : ""}${issue}.`;
  return zh ? `${failure ? `${failure}，` : ""}${issue}，请对照原文件补全。`
    : `${failure ? `${failure}. ` : ""}${issue}. Complete it using the original.`;
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
    material_parser_provider_required: ["请为资料解析选择文本模型。", "Choose a text model for material parsing."],
    material_recognition_review_required: ["资料识别结果尚未核实，请对照原文件核对。", "Check the recognized material against the original."],
    default_max_score_requires_review: ["当前使用默认 10 分，请确认题目满分", "Default 10-point maximum; confirm the score"],
    max_score_not_found: ["未从每题分值说明中匹配到本题，已暂按 10 分", "No score matched this question; temporarily set to 10"],
    recognition_partial: ["识别覆盖或内容尚待核对，请对照原文", "Recognition coverage or content needs review against the source"],
    recognition_needs_review: ["识别结果存在不确定内容，请对照原文", "Uncertain recognition content; compare with the source"],
  };
  const label = labels[code];
  return label ? label[locale === "zh-CN" ? 0 : 1]
    : (locale === "zh-CN" ? "资料存在待核对项，请打开详情" : "Material needs review; open the details");
}

export function reviewProviderFailureLabel(code: unknown, status: unknown, locale: string): string | undefined {
  const labels: Record<string, [string, string]> = {
    provider_upstream_unavailable: ["模型服务暂时不可用", "The model service is temporarily unavailable"],
    provider_timeout: ["模型响应超时", "The model response timed out"],
    provider_unreachable: ["模型连接失败或中断", "The model connection failed or was interrupted"],
    provider_rate_limited: ["模型请求受到限流", "The model request was rate limited"],
    provider_response_invalid: ["模型返回的结果格式无法使用", "The model response format could not be used"],
    recognition_response_invalid: ["识别结果未通过校验", "The recognition response failed validation"],
    provider_recitation_blocked: ["模型因原文复述限制停止输出", "The model stopped under its recitation policy"],
    provider_content_blocked: ["模型因内容策略停止输出", "The model stopped under its content policy"],
  };
  if (typeof code !== "string" || !Object.hasOwn(labels, code)) return undefined;
  const zh = locale === "zh-CN";
  const httpStatus = typeof status === "number" && Number.isInteger(status)
    && ((code === "provider_upstream_unavailable" && status >= 500 && status <= 599)
      || (code === "provider_rate_limited" && status === 429)) ? status : null;
  return labels[code][zh ? 0 : 1] + (httpStatus ? zh ? `（HTTP ${httpStatus}）` : ` (HTTP ${httpStatus})` : "");
}

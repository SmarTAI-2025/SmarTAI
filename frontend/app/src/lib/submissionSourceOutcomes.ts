import type { Locale } from "@/i18n/messages";
import type { SubmissionSourceOutcome } from "@/types";

export interface SubmissionSourceReasonCopy {
  title: string;
  description: string;
  nextStep?: string;
}

export function getSubmissionSourceReasonCopy(
  source: SubmissionSourceOutcome,
  locale: Locale,
): SubmissionSourceReasonCopy {
  const copy = REASON_COPY[source.reason_code ?? source.internal_status];
  if (copy) return copy(locale, source);

  if (source.status === "processing") {
    return {
      title: tx(locale, "等待处理", "Waiting to be processed"),
      description: tx(locale, "原文件已保存，正在等待读取或识别。", "The original file is saved and waiting for extraction or recognition."),
    };
  }
  if (source.status === "parsed") {
    return parsedCopy(locale, source);
  }
  return {
    title: tx(locale, "这份作答未完成识别", "This submission was not recognized"),
    description: tx(
      locale,
      "后端保留了原文件和诊断代码，但没有得到可用作答。可按下方错误代码重试；若重复出现，请把任务编号交给管理员排查。",
      "The backend preserved the original file and diagnostic code but produced no usable answers. Retry using the code below; if it repeats, share the job ID with an administrator.",
    ),
  };
}

type ReasonFactory = (locale: Locale, source: SubmissionSourceOutcome) => SubmissionSourceReasonCopy;

const REASON_COPY: Record<string, ReasonFactory> = {
  workflow_failed: (locale) => ({
    title: tx(locale, "识别任务意外终止", "The recognition task stopped unexpectedly"),
    description: tx(locale, "系统已停止本次任务并保留原文件；没有把未确认的结果继续显示为处理中或送入批改。", "The run stopped and the originals were preserved. Unconfirmed results were not left processing or sent to grading."),
    nextStep: tx(locale, "先重试一次；若再次出现，请携带任务编号让管理员查看服务端日志。", "Retry once. If it happens again, share the job ID with an administrator to inspect server logs."),
  }),
  no_provider_configured: (locale) => ({
    title: tx(locale, "尚未配置作答识别模型", "No submission-recognition model is configured"),
    description: tx(locale, "系统没有可用于读取并结构化学生作答的已启用模型，因此没有开始猜测或生成结果。", "No enabled model is available to read and structure student submissions, so the system did not guess or generate results."),
    nextStep: tx(locale, "先在模型设置中启用并选择一个识别模型，再重试这批原文件。", "Enable and select a recognition model in model settings, then retry these originals."),
  }),
  provider_not_enabled: (locale) => ({
    title: tx(locale, "所选模型已停用", "The selected model is disabled"),
    description: tx(locale, "任务引用的模型配置仍然存在，但当前不可用；原文件没有因此被当成识别成功。", "The referenced model configuration still exists but is currently unavailable. The originals were not treated as successfully recognized."),
    nextStep: tx(locale, "重新启用该模型，或改选另一个已启用模型后重试。", "Re-enable this model or select another enabled model, then retry."),
  }),
  recognition_provider_not_enabled: (locale) => ({
    title: tx(locale, "作答识别模型已停用", "The submission-recognition model is disabled"),
    description: tx(locale, "本任务指定的作答识别模型当前不可用，因此 OCR/结构识别没有继续执行。", "The recognition model selected for this task is unavailable, so OCR and structured recognition did not continue."),
    nextStep: tx(locale, "在任务设置中改选已启用且与文件类型匹配的模型后重试。", "Choose an enabled model that supports the file type in task settings, then retry."),
  }),
  vision_provider_required: (locale) => ({
    title: tx(locale, "当前模型不支持图片 OCR", "The selected model cannot OCR images"),
    description: tx(
      locale,
      "这份文件需要读取图片或扫描页，但当前识别模型不支持图片输入。原文件已保存，并不是文件丢失或学生答案有误。",
      "This file requires image or scanned-page reading, but the selected recognition model does not accept image input. The original is saved; the file was not lost and the student's answer is not at fault.",
    ),
    nextStep: tx(locale, "改用支持图片输入的视觉模型，或上传可复制文字版 PDF/TXT。", "Choose a vision-capable model, or upload a text-based PDF/TXT file."),
  }),
  ocr_empty_result: (locale) => ({
    title: tx(locale, "OCR 没有读到可用文字", "OCR found no usable text"),
    description: tx(
      locale,
      "模型完成了图片读取，但返回内容为空。常见原因是照片模糊、方向错误、反光、裁切不全或页面本身空白。",
      "The image request completed, but OCR returned no text. Common causes include blur, wrong orientation, glare, cropping, or a blank page.",
    ),
    nextStep: tx(locale, "检查清晰度和方向后重新拍摄；也可以换一个视觉模型重试。", "Check clarity and orientation, rescan the page, or retry with another vision model."),
  }),
  provider_timeout: (locale, source) => providerCopy(locale, source, "模型响应超时", "The model timed out", "稍后重试；原文件已经保存，不需要重新整理整批文件。", "Retry later. The original is saved, so the whole batch does not need to be rebuilt."),
  provider_unreachable: (locale, source) => providerCopy(locale, source, "暂时无法连接模型服务", "The model service is unreachable", "检查网络或模型服务状态后重试。", "Check network and provider status, then retry."),
  provider_rate_limited: (locale, source) => providerCopy(locale, source, "模型服务触发限流", "The model service rate-limited the request", "等待限额恢复后重试，或换用另一个已启用模型。", "Wait for the quota window or retry with another enabled model."),
  provider_auth_failed: (locale, source) => providerCopy(locale, source, "模型凭据无效或无权限", "The model credentials were rejected", "到 BYOK 检查 API Key、模型权限和余额后重试。", "Check the API key, model permission, and balance in BYOK, then retry."),
  provider_credentials_unavailable: (locale, source) => providerCopy(locale, source, "无法读取已保存的模型凭据", "Saved model credentials could not be loaded", "重新保存 BYOK 凭据后重试。", "Save the BYOK credentials again, then retry."),
  submission_parse_invalid: (locale) => ({
    title: tx(locale, "模型返回格式无法解析", "The model returned an invalid structure"),
    description: tx(
      locale,
      "模型有返回内容，但没有形成系统需要的学生、题号和作答结构；系统没有把不确定内容当成成功结果。",
      "The model returned content, but not the required student, question-ID, and answer structure. The system did not treat uncertain output as a success.",
    ),
    nextStep: tx(locale, "可直接重试；若持续出现，换用结构化输出更稳定的模型。", "Retry once; if it persists, choose a model with more reliable structured output."),
  }),
  submission_parse_failed: (locale) => ({
    title: tx(locale, "作答识别没有完成", "Submission recognition did not complete"),
    description: tx(
      locale,
      "系统没有得到可用的结构化作答，也没有收到更具体的安全原因码。原文件与任务编号已保留，可据此继续排查。",
      "No usable structured submission was produced, and no more specific safe code was available. The original file and job ID are preserved for diagnosis.",
    ),
    nextStep: tx(locale, "先重试一次；若仍失败，请携带下方任务编号排查服务端日志。", "Retry once; if it fails again, use the job ID below to inspect server logs."),
  }),
  submission_model_field_too_long: (locale) => ({
    title: tx(locale, "模型返回字段超过安全长度", "The model returned an oversized field"),
    description: tx(
      locale,
      "模型返回了过长的学号、姓名、题号或标签。系统只拒绝这份来源，没有让它拖垮整批，也没有截断后冒充正确结果。",
      "The model returned an overlong student ID, name, question ID, or flag. Only this source was rejected; the batch continued and no truncated value was treated as correct.",
    ),
    nextStep: tx(locale, "重试一次；若持续出现，换用结构化输出更稳定的模型并核对原文件版式。", "Retry once; if it persists, use a model with more reliable structured output and inspect the source layout."),
  }),
  submission_source_persistence_failed: (locale) => ({
    title: tx(locale, "原文件保存未完成", "The original file was not saved"),
    description: tx(
      locale,
      "系统无法确认这份原文件已经安全保存，因此没有继续把它当作可识别来源。这是存储或数据库问题，不是学生答案问题。",
      "The system could not confirm that this original was safely stored, so it did not continue treating it as a recognizable source. This is a storage or database issue, not a student-answer issue.",
    ),
    nextStep: tx(locale, "先重试上传；若重复出现，请携带任务编号让管理员检查文件存储和数据库。", "Retry the upload. If it repeats, share the job ID with an administrator to check file storage and the database."),
  }),
  submission_persistence_failed: (locale) => ({
    title: tx(locale, "识别结果写入任务失败", "Recognized results could not be saved to the task"),
    description: tx(
      locale,
      "原文件和逐文件识别结果已经保留，但系统未能把可用作答发布到当前任务。这是结果持久化问题，不应重新解释为 OCR 或学生答案错误。",
      "The originals and per-file recognition outcomes were preserved, but usable answers could not be published to this task. This is result persistence failure, not an OCR or student-answer error.",
    ),
    nextStep: tx(locale, "请携带任务编号让管理员检查数据库冲突或存储状态，然后重试。", "Share the job ID with an administrator to check database conflicts or storage state, then retry."),
  }),
  submission_outcome_persistence_failed: (locale) => ({
    title: tx(locale, "逐文件识别结果保存未完成", "Per-file outcomes were not fully saved"),
    description: tx(
      locale,
      "原文件已经保存，但系统无法确认每份文件的终态都写入数据库。任务已停止，不会把缺失结果继续显示为处理中，也不会静默进入批改。",
      "The originals were saved, but the system could not confirm that every per-file terminal outcome reached the database. The task stopped; missing results will not remain processing or silently enter grading.",
    ),
    nextStep: tx(locale, "携带任务编号让管理员检查数据库后重试；无需重新整理原始文件。", "Share the job ID with an administrator to check the database, then retry; the originals do not need to be rebuilt."),
  }),
  no_answer_content_detected: (locale) => ({
    title: tx(locale, "没有提取到任何作答内容", "No answer content was detected"),
    description: tx(
      locale,
      "文件文字和模型返回结构都可以读取，但结果中没有任何学生作答。这与“题号不匹配”不同，也不表示学生答错；常见原因是只上传了封面/空白页、答案区域未拍全，或页面版式让模型漏掉了作答。",
      "The file text and model response structure were readable, but no student answers were present in the result. This differs from unmatched question IDs and does not mean the student answered incorrectly. Common causes include a cover/blank page, a cropped answer area, or a layout the model missed.",
    ),
    nextStep: tx(locale, "打开原文件确认答案区域存在且完整；必要时重新拍摄，或换一个识别模型重试。", "Open the original and confirm the answer area is present and complete; rescan or retry with another recognition model if needed."),
  }),
  no_matching_answer: (locale, source) => ({
    title: tx(locale, "没有作答能对应当前任务题目", "No answers match this task's questions"),
    description: tx(
      locale,
      "文件内容已被解析，也提取到了作答条目，但其中的题号没有一个能对应当前任务。它表示“题目不匹配”，不是“学生答错了”。可能是传错作业、题号被 OCR 误读，或任务题目后来被替换。",
      "The file was parsed and answer entries were extracted, but none of their question IDs match this task. This means the questions do not match—not that the student answered incorrectly. The wrong assignment may have been uploaded, OCR may have misread labels, or the task questions may have changed.",
    ),
    nextStep: source.unknown_question_ids.length
      ? tx(locale, `识别到的未匹配题号：${source.unknown_question_ids.join("、")}。请核对原文件与当前题目。`, `Unmatched IDs found: ${source.unknown_question_ids.join(", ")}. Compare the original file with the current questions.`)
      : tx(locale, "打开原文件核对是否属于本次作业，并检查题号是否清晰。", "Open the original and verify that it belongs to this assignment and has readable question labels."),
  }),
  duplicate_student_identity: (locale, source) => ({
    title: tx(locale, "多份文件识别成同一位学生", "Multiple files resolve to the same student"),
    description: tx(
      locale,
      `至少两份原文件都识别为${source.student_candidate ? `“${source.student_candidate}”` : "同一身份"}。系统保留了每一份文件，没有用后一份覆盖前一份。`,
      `At least two originals resolved to ${source.student_candidate ? `“${source.student_candidate}”` : "the same identity"}. Every file was preserved; later files did not overwrite earlier ones.`,
    ),
    nextStep: tx(locale, "逐份核对姓名/学号，为错误的一份修正身份后再继续。", "Review each file and correct the student identity before continuing."),
  }),
  identity_needs_review: (locale, source) => ({
    title: tx(locale, "学生身份需要教师确认", "Student identity needs teacher review"),
    description: tx(
      locale,
      source.student_candidate
        ? `系统提取到候选身份“${source.student_candidate}”，但无法唯一确认。作答内容已保留。`
        : "系统读取到了作答，但无法从文件名、正文或名单中唯一确认学生身份。",
      source.student_candidate
        ? `The system found candidate “${source.student_candidate}” but could not confirm it uniquely. The answers are preserved.`
        : "The answers were read, but the student could not be uniquely confirmed from the filename, content, or roster.",
    ),
    nextStep: tx(locale, "核对原文件并确认或修正学号和姓名。", "Compare the original and confirm or correct the student ID and name."),
  }),
  student_identity_conflict: (locale, source) => ({
    title: tx(locale, "学生身份发生冲突", "The student identity conflicts"),
    description: tx(
      locale,
      source.student_candidate
        ? `候选身份“${source.student_candidate}”与当前任务中的其他身份记录冲突，作答和原文件均已保留。`
        : "系统读取到了作答，但身份记录与当前任务中的其他记录冲突。",
      source.student_candidate
        ? `Candidate “${source.student_candidate}” conflicts with another identity in this task. The answers and original are preserved.`
        : "The answers were read, but the identity conflicts with another record in this task.",
    ),
    nextStep: tx(locale, "逐份核对原文件并确认正确学号和姓名。", "Review the originals and confirm the correct student ID and name."),
  }),
  source_decode_failed: (locale) => fileCopy(locale, "文本编码无法读取", "The text encoding could not be read", "请把文本另存为 UTF-8，或上传 PDF/图片版本。", "Save the text as UTF-8, or upload a PDF/image version."),
  submission_source_content_type_mismatch: (locale) => fileCopy(locale, "文件内容与扩展名或类型不一致", "The file contents do not match its name or declared type", "不要只修改扩展名；请从原应用重新导出为受支持的 PDF、TXT、JPG、PNG 或 WebP 后上传。", "Do not only rename the extension. Export the file again as a supported PDF, TXT, JPG, PNG, or WebP, then upload it."),
  submission_source_empty: (locale) => fileCopy(locale, "文件没有可识别内容", "The file contains no recognizable content", "检查是否为空白页、空文件或只包含不可见字符。", "Check for a blank page, empty file, or invisible-only content."),
  submission_source_unsupported: (locale) => fileCopy(locale, "文件类型暂不支持", "This file type is not supported", "转换为 PDF、TXT、Markdown、CSV、JPG、PNG 或 WebP 后重新上传。", "Convert it to PDF, TXT, Markdown, CSV, JPG, PNG, or WebP and upload again."),
  submission_source_too_large: (locale) => fileCopy(locale, "文件超过处理上限", "The file exceeds the processing limit", "压缩图片，或拆分 PDF/压缩包后重新上传。", "Compress the image or split the PDF/archive, then upload again."),
  pdf_processing_unavailable: (locale) => fileCopy(locale, "服务器暂时不能处理 PDF", "PDF processing is unavailable on the server", "请管理员恢复 PDF 处理组件；也可临时转为图片或 TXT。", "Ask an administrator to restore PDF processing, or convert the file to an image/TXT temporarily."),
  pdf_extraction_busy: (locale) => fileCopy(locale, "PDF 读取服务正忙", "PDF extraction is busy", "稍等片刻后直接重试；原文件已经保存。", "Wait briefly and retry; the original file is already saved."),
  pdf_extraction_timeout: (locale) => fileCopy(locale, "PDF 读取超时", "PDF extraction timed out", "拆分或优化 PDF 后重试。", "Split or optimize the PDF, then retry."),
  pdf_page_limit_exceeded: (locale) => fileCopy(locale, "PDF 页数超过上限", "The PDF exceeds the page limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_character_limit_exceeded: (locale) => fileCopy(locale, "PDF 文字量超过上限", "The PDF exceeds the text limit", "拆分 PDF 后重新上传。", "Split the PDF and upload again."),
  pdf_extraction_failed: (locale) => fileCopy(locale, "PDF 内容无法读取", "The PDF could not be read", "检查文件是否损坏或加密，并尝试重新导出。", "Check whether the file is damaged or encrypted, and export it again."),
  pdf_ocr_render_failed: (locale) => fileCopy(locale, "扫描 PDF 无法转成 OCR 页面", "The scanned PDF could not be rendered for OCR", "尝试重新导出 PDF，或逐页转为清晰图片。", "Export the PDF again, or convert each page to a clear image."),
  submission_archive_empty: (locale) => archiveCopy(locale, "压缩包中没有学生文件", "The archive contains no student files", "确认压缩包不是空的，且文件没有全部放在系统隐藏目录中。", "Make sure the archive is not empty and files are not all in hidden system folders."),
  submission_archive_invalid: (locale) => archiveCopy(locale, "压缩包损坏或结构不安全", "The archive is damaged or unsafe", "重新打包为 ZIP，并避免加密、路径穿越或损坏条目。", "Create a new ZIP without encryption, unsafe paths, or damaged members."),
  submission_archive_limit_exceeded: (locale) => archiveCopy(locale, "压缩包超过安全上限", "The archive exceeds a safety limit", "减少文件数量、单文件大小或解压后总体积，再重新上传。", "Reduce file count, member size, or total expanded size, then upload again."),
  submission_archive_member_too_large: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件过大", "This archive member is too large", "只压缩或拆分这份文件后重新打包；同包其他健康文件已经继续处理。", "Compress or split only this member, then rebuild the archive. Other healthy members continued processing."),
  submission_archive_member_unreadable: (locale) => archiveMemberCopy(locale, "压缩包中的这份文件无法读取", "This archive member could not be read", "重新导出这份文件并重新打包；系统保留了整个原始压缩包供核对。", "Export this member again and rebuild the archive. The original container was preserved for review."),
  submission_archive_member_unsafe_path: (locale) => archiveMemberCopy(locale, "压缩包成员路径不安全", "This archive member has an unsafe path", "重新打包时移除绝对路径和 ../ 上级目录；同包其他安全文件已经继续处理。", "Rebuild the archive without absolute paths or ../ parent traversal. Other safe members continued processing."),
};

function parsedCopy(locale: Locale, source: SubmissionSourceOutcome): SubmissionSourceReasonCopy {
  return {
    title: source.unknown_question_ids.length
      ? tx(locale, "已识别，部分题号未匹配", "Recognized with some unmatched question IDs")
      : tx(locale, "识别成功", "Recognized"),
    description: tx(
      locale,
      `已对应 ${source.matched_answer_count} 道当前任务题目。`,
      `${source.matched_answer_count} answer(s) matched the current task.`,
    ),
    nextStep: source.unknown_question_ids.length
      ? tx(locale, `未匹配题号：${source.unknown_question_ids.join("、")}。`, `Unmatched IDs: ${source.unknown_question_ids.join(", ")}.`)
      : undefined,
  };
}

function providerCopy(locale: Locale, source: SubmissionSourceOutcome, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  const isOcr = source.failure_phase === "ocr";
  return {
    title: tx(locale, isOcr ? `OCR：${zhTitle}` : zhTitle, isOcr ? `OCR: ${enTitle}` : enTitle),
    description: tx(locale, isOcr ? "原文件已保存，但本次 OCR 模型调用没有得到可用结果。" : "原文件已保存，但本次模型调用没有得到可用结果。", isOcr ? "The original is saved, but this OCR model call produced no usable result." : "The original file is saved, but this model call produced no usable result."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function fileCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "原文件已保存，但读取阶段没有得到可供识别的正文。", "The original is saved, but the read stage produced no usable text."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function archiveCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "系统没有把压缩包中的文件当成成功结果，批次原文件仍然保留。", "No archive members were treated as successful results; the uploaded batch file is preserved."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function archiveMemberCopy(locale: Locale, zhTitle: string, enTitle: string, zhStep: string, enStep: string): SubmissionSourceReasonCopy {
  return {
    title: tx(locale, zhTitle, enTitle),
    description: tx(locale, "这一个成员没有被当成成功结果；整个原始压缩包已保存，同包其他成员不会因此丢失。", "This member was not treated as successful. The original container is saved, and other members are not discarded because of it."),
    nextStep: tx(locale, zhStep, enStep),
  };
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "en-US" ? en : zh;
}

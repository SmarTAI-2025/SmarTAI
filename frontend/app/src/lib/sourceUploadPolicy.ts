import type { PreparationSourceRole } from "@/types";

// Mirror task_preparation._SOURCE_ROLE_EXTENSIONS and the course-library policy.
export const DOCUMENT_SOURCE_EXTENSIONS = [".pdf", ".txt", ".md", ".markdown"] as const;
export const IMAGE_SOURCE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"] as const;
export const KNOWLEDGE_SOURCE_EXTENSIONS = [...DOCUMENT_SOURCE_EXTENSIONS, ...IMAGE_SOURCE_EXTENSIONS];
export const MAX_KNOWLEDGE_UPLOAD_BYTES = 64 * 1024 * 1024;
export const MAX_KNOWLEDGE_IMAGE_BYTES = 10 * 1024 * 1024;
export const MAX_SOURCE_UPLOAD_BYTES = 5 * 1024 * 1024;

export function sourceUploadExtensions(role: PreparationSourceRole): string[] {
  return [...DOCUMENT_SOURCE_EXTENSIONS, ...(role === "programming_tests" ? [".json"] : []), ...IMAGE_SOURCE_EXTENSIONS];
}

export function matchesUploadExtension(filename: string, extensions: readonly string[]): boolean {
  const name = filename.trim().toLowerCase();
  return extensions.some(extension => name.endsWith(extension));
}

export function canSaveSourceToLibrary(filename: string): boolean {
  return matchesUploadExtension(filename, KNOWLEDGE_SOURCE_EXTENSIONS);
}

export function librarySourceFormatHint(locale: string): string {
  return locale === "zh-CN"
    ? "课程资料库支持 PDF、TXT、Markdown 和静态图片。图片需视觉/OCR 模型后台识别后才可检索，可能产生额外模型费用；JSON/CSV 不能同时存入资料库。"
    : "The course library supports PDF, TXT, Markdown and static images. Images require background vision/OCR indexing before search and may incur additional model charges. JSON/CSV cannot also be saved to the library.";
}

export function sourceUploadFormatHint(extensions: readonly string[], maxBytes: number, locale: string): string {
  const formats = extensions.map(extension => extension.slice(1).toUpperCase()).join(" / ");
  const size = `${Number((maxBytes / 1024 / 1024).toFixed(1))} MiB`;
  return locale === "zh-CN" ? `${formats} · 单个文件最大 ${size}` : `${formats} · Maximum ${size} per file`;
}

export function sourceUploadValidationError(file: File, extensions: readonly string[], maxBytes: number, locale: string): string | null {
  if (file.size === 0) return locale === "zh-CN"
    ? "文件为空（0 字节），请选择包含题目或资料内容的文件。"
    : "This file is empty (0 bytes). Choose a file containing questions or supporting material.";
  if (file.size > maxBytes) return locale === "zh-CN"
    ? `文件超过单个文件 ${Number((maxBytes / 1024 / 1024).toFixed(1))} MiB 的上传上限，请选择更小的文件。`
    : `This file exceeds the ${Number((maxBytes / 1024 / 1024).toFixed(1))} MiB per-file upload limit. Choose a smaller file.`;
  if (matchesUploadExtension(file.name, extensions)) return null;
  const formats = extensions.map(extension => extension.slice(1).toUpperCase()).join(" / ");
  return locale === "zh-CN"
    ? `当前资料类型不支持此文件。可用格式：${formats}。请导出为支持的格式，不要只修改扩展名。`
    : `This file is not supported for the selected material type. Available formats: ${formats}. Export to a supported format; do not just rename the extension.`;
}

export function knowledgeUploadMaxBytes(filename: string): number {
  return matchesUploadExtension(filename, IMAGE_SOURCE_EXTENSIONS) ? MAX_KNOWLEDGE_IMAGE_BYTES : MAX_KNOWLEDGE_UPLOAD_BYTES;
}

export function knowledgeUploadFormatHint(locale: string): string {
  return locale === "zh-CN"
    ? "PDF / TXT / MD / MARKDOWN（64 MiB）；静态 JPG / JPEG / PNG / BMP / TIF / TIFF / WEBP（10 MiB）"
    : "PDF / TXT / MD / MARKDOWN (64 MiB); static JPG / JPEG / PNG / BMP / TIF / TIFF / WEBP (10 MiB)";
}

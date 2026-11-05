import { describe, expect, it } from "vitest";
import { canSaveSourceToLibrary, KNOWLEDGE_SOURCE_EXTENSIONS, IMAGE_SOURCE_EXTENSIONS, knowledgeUploadMaxBytes, MAX_SOURCE_UPLOAD_BYTES, sourceUploadExtensions, sourceUploadValidationError } from "./sourceUploadPolicy";

describe("source upload contracts", () => {
  it("keeps the library and task image format sets aligned with decoder limits", () => {
    expect(KNOWLEDGE_SOURCE_EXTENSIONS).toEqual(sourceUploadExtensions("problem"));
    for (const extension of IMAGE_SOURCE_EXTENSIONS) {
      expect(canSaveSourceToLibrary(`scan${extension.toUpperCase()}`)).toBe(true);
      expect(knowledgeUploadMaxBytes(`scan${extension}`)).toBe(10 * 1024 * 1024);
    }
    expect(knowledgeUploadMaxBytes("book.pdf")).toBe(64 * 1024 * 1024);
    expect(canSaveSourceToLibrary("cases.csv")).toBe(false);
  });
  it.each(["problem", "reference_answer", "rubric", "programming_tests"] as const)("keeps all supported images selectable for %s", role => {
    expect(sourceUploadExtensions(role)).toEqual(expect.arrayContaining([".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"]));
    expect(sourceUploadExtensions(role).includes(".json")).toBe(role === "programming_tests");
  });
  it("allows JSON only when importing test cases and never stores it in the library", () => {
    const file = new File(["{}"], "cases.JSON");
    expect(sourceUploadValidationError(file, sourceUploadExtensions("programming_tests"), MAX_SOURCE_UPLOAD_BYTES, "zh-CN")).toBeNull();
    expect(sourceUploadValidationError(file, sourceUploadExtensions("rubric"), MAX_SOURCE_UPLOAD_BYTES, "zh-CN")).toContain("不支持");
    expect(canSaveSourceToLibrary(file.name)).toBe(false);
    expect(canSaveSourceToLibrary("scan.png")).toBe(true);
    expect(canSaveSourceToLibrary("notes.MARKDOWN")).toBe(true);
  });
  it.each(["zh-CN", "en-US"])("rejects zero and oversized files before upload (%s)", locale => {
    const accepted = sourceUploadExtensions("problem");
    expect(sourceUploadValidationError(new File([], "empty.txt"), accepted, MAX_SOURCE_UPLOAD_BYTES, locale)).toContain(locale === "zh-CN" ? "0 字节" : "0 bytes");
    const file = new File(["x"], "large.txt");
    Object.defineProperty(file, "size", { value: MAX_SOURCE_UPLOAD_BYTES + 1 });
    expect(sourceUploadValidationError(file, accepted, MAX_SOURCE_UPLOAD_BYTES, locale)).toContain("5 MiB");
  });
});

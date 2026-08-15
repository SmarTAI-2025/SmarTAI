import { describe, expect, it } from "vitest";
import { inferSourcePreviewKind } from "./sourcePreview";

describe("source preview type inference", () => {
  it("recognizes supported file names without loading file content", () => {
    expect(inferSourcePreviewKind("question-set.pdf")).toBe("pdf");
    expect(inferSourcePreviewKind("student-answer.PNG")).toBe("image");
  });

  it("prefers an explicit supported MIME type", () => {
    expect(inferSourcePreviewKind("download", "image/webp")).toBe("image");
    expect(inferSourcePreviewKind("download", "application/pdf")).toBe("pdf");
  });

  it("rejects unsupported active-content formats", () => {
    expect(inferSourcePreviewKind("answer.svg", "image/svg+xml")).toBe("unsupported");
    expect(inferSourcePreviewKind("answer.html", "text/html")).toBe("unsupported");
  });
});

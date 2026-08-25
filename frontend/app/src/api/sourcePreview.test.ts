import { describe, expect, it, vi } from "vitest";
import {
  loadSourcePreviewFile,
  SOURCE_PREVIEW_NOT_CONNECTED,
  sourcePreviewErrorCode,
} from "./sourcePreview";

describe("source preview adapter", () => {
  it("fails with a stable not-connected code without guessing a backend route", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    await expect(loadSourcePreviewFile(null)).rejects.toMatchObject({
      code: SOURCE_PREVIEW_NOT_CONNECTED,
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("normalizes unexpected adapter failures", () => {
    expect(sourcePreviewErrorCode(new Error("network"))).toBe("source_preview_load_failed");
  });
});

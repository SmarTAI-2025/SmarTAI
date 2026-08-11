import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useFrontierDemoSourcePreview } from "./useFrontierDemoSourcePreview";

describe("useFrontierDemoSourcePreview", () => {
  it("opens the bundled question PDF directly without a transient blob URL", () => {
    const { result } = renderHook(() => useFrontierDemoSourcePreview({
      enabled: true,
      questionSource: true,
    }));

    expect(result.current.available).toBe(true);
    expect(result.current.previewUrl).toBe("/frontier-demo/live/question_source.pdf");
    act(() => result.current.openPreview());
    expect(result.current.open).toBe(true);
    expect(result.current.loadState).toBe("ready");
  });

  it("keeps unknown submission files unavailable", () => {
    const { result } = renderHook(() => useFrontierDemoSourcePreview({
      enabled: true,
      sourceFilename: "not-a-demo-file.pdf",
    }));

    expect(result.current.available).toBe(false);
    expect(result.current.previewUrl).toBeNull();
    act(() => result.current.openPreview());
    expect(result.current.loadState).toBe("error");
  });

  it("maps a named handwritten fixture to the exact PNG uploaded for OCR", () => {
    const { result } = renderHook(() => useFrontierDemoSourcePreview({
      enabled: true,
      sourceFilename: "DEMO-002_Maya-Lin_handwritten.png",
    }));

    expect(result.current.available).toBe(true);
    expect(result.current.previewUrl).toBe("/frontier-demo/live/DEMO-002_handwritten_raw.png");
    expect(result.current.descriptor.preview_kind).toBe("image");
    expect(result.current.descriptor.mime_type).toBe("image/png");
  });
});

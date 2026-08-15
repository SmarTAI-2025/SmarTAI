import { afterEach, describe, expect, it, vi } from "vitest";
import { buildSourcePreviewMockScenario } from "@/lib/sourcePreview";

describe("source preview prelaunch mock contract", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("stays available in a production-mode prelaunch build without a feature flag", () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("MODE", "production");

    const scenario = buildSourcePreviewMockScenario({
      scope: "problem",
      sourceId: "preview-build-contract",
      displayName: "question-source.pdf",
      taskFinalized: false,
      variant: "pdf",
    });

    expect(scenario.descriptor).toMatchObject({
      status: "available",
      preview_kind: "pdf",
      unavailable_reason: null,
    });
  });
});

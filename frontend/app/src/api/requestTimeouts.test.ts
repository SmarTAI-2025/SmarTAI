import { afterEach, expect, it, vi } from "vitest";
import { apiClient, postMultipart } from "./client";
import { uploadReference, uploadTestCases } from "./tasks";

afterEach(() => vi.restoreAllMocks());

it.each([uploadReference, uploadTestCases])("allows auxiliary uploads the material preflight wait", async (upload) => {
  const post = vi.spyOn(apiClient, "post").mockResolvedValue({ data: { status: "started" } });
  const file = new File(["text"], "source.txt");
  await upload("task", file, { fields: { extra: "kept" } });
  expect(post.mock.calls[0][2]).toEqual(expect.objectContaining({ timeout: 1_020_000 }));
  expect((post.mock.calls[0][1] as FormData).get("extra")).toBe("kept");
});

it("preserves the ordinary multipart launch wait", async () => {
  const post = vi.spyOn(apiClient, "post").mockResolvedValue({ data: {} });
  await postMultipart("/tasks/task/parse_submissions", null);
  expect(post.mock.calls[0][2]).toEqual(expect.objectContaining({ timeout: 180_000 }));
});


it("preflight wait includes the server processing and worst PDF cleanup allowance", async () => {
  const { SOURCE_PREFLIGHT_TIMEOUT_MS } = await import("./requestTimeouts");
  expect(SOURCE_PREFLIGHT_TIMEOUT_MS).toBeGreaterThanOrEqual((950 + 60 + 10) * 1000);
});

it("preserves source preflight data while allowing bounded cleanup", async () => {
  const { preflightProblemSource } = await import("./problemSources");
  const { preflightMaterialImport } = await import("./materialImports");
  const post = vi.spyOn(apiClient, "post").mockResolvedValue({ data: { status: "ready" } });
  await preflightProblemSource({ taskId: "task", mode: "inline_text", inlineText: "Question",
    structureMode: "organized", extractionHint: "", saveToLibrary: false, recognitionProviderId: "provider" });
  expect(post.mock.calls[0][2]).toEqual({ timeout: 1_020_000 });
  expect((post.mock.calls[0][1] as FormData).get("inline_text")).toBe("Question");
  await preflightMaterialImport({ taskId: "task", file: new File(["text"], "rubric.txt"), targets: ["criterion"],
    structureMode: "organized", extractionHint: "", saveToLibrary: false });
  expect(post.mock.calls[1][2]).toEqual({ timeout: 1_020_000 });
  expect((post.mock.calls[1][1] as FormData).get("targets")).toBe('["criterion"]');
});

import { describe, expect, it } from "vitest";
import { APIError } from "@/api/client";
import {
  classifyRecoverableError,
  isCurrentResultArtifactReady,
  isWorkflowRevisionConflictCode,
} from "./taskActionGuards";

describe("task contract compatibility", () => {
  it.each(["stale_revision", "task_workflow_changed", "version_conflict"])(
    "treats %s as a workflow revision conflict",
    (code) => {
      expect(isWorkflowRevisionConflictCode(code)).toBe(true);
    },
  );

  it("does not expose current downloads while the formal result is dirty", () => {
    expect(isCurrentResultArtifactReady({
      finalResultDirty: true,
      status: "ready",
      fileCount: 5,
    })).toBe(false);
  });

  it("exposes ready artifacts only for a clean version with files", () => {
    expect(isCurrentResultArtifactReady({
      finalResultDirty: false,
      status: "ready",
      fileCount: 5,
    })).toBe(true);
    expect(isCurrentResultArtifactReady({
      finalResultDirty: false,
      status: "ready",
      fileCount: 0,
    })).toBe(false);
  });
});

describe("question source recovery guidance", () => {
  it("routes a missing vision provider to BYOK before a job starts", () => {
    const info = classifyRecoverableError(
      new APIError(422, "vision_provider_required", {
        detail: { code: "vision_provider_required" },
      }),
      { locale: "zh-CN", returnTo: "/tasks/task-1/upload/problems" },
    );

    expect(info.actionKind).toBe("byok");
    expect(info.actionHref).toContain("/settings/byok");
    expect(info.description).toContain("BYOK");
  });

  it("routes role or MIME rejection back to file selection", () => {
    const info = classifyRecoverableError(
      new APIError(415, "source_type_not_allowed", {
        detail: { code: "source_type_not_allowed" },
      }),
      { locale: "zh-CN" },
    );

    expect(info.actionKind).toBe("reupload");
    expect(info.actionLabel).toBe("重新选择文件");
  });
});

describe("background task failure guidance", () => {
  it("keeps a stable grading failure code and job id visible", () => {
    const info = classifyRecoverableError("grading_failed", {
      locale: "zh-CN",
      phase: "error",
      jobId: "run-1",
    });

    expect(info.title).toBe("本次批改没有完成");
    expect(info.description).toContain("任务资料仍然保留");
    expect(info.technicalDetails).toContainEqual({ label: "错误代码", value: "grading_failed" });
    expect(info.technicalDetails).toContainEqual({ label: "任务编号", value: "run-1" });
  });
});

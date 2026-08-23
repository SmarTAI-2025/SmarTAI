import { describe, expect, it } from "vitest";
import { APIError } from "@/api/client";
import {
  backgroundErrorTitle,
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
    expect(info.description).toContain("没有可用的视觉模型");
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

  it("explains the exact file-size limit instead of reporting a format problem", () => {
    const info = classifyRecoverableError(
      new APIError(413, "source_too_large", {
        detail: { code: "source_too_large", max_bytes: 5 * 1024 * 1024 },
      }),
      { locale: "zh-CN" },
    );

    expect(info.actionKind).toBe("reupload");
    expect(info.description).toContain("5 MB");
    expect(info.description).not.toContain("格式");
    expect(info.technicalDetails).toContainEqual({ label: "文件上限", value: "5 MB" });
  });
});

describe("background task failure guidance", () => {
  it("explains a provider timeout without collapsing it into a generic failure", () => {
    const info = classifyRecoverableError("provider_timeout", {
      locale: "zh-CN",
      phase: "question_preparation",
      jobId: "op-timeout",
    });

    expect(info.title).toBe("模型响应超时");
    expect(info.description).toContain("稍后重试");
    expect(info.actionKind).toBe("retry");
    expect(info.tone).toBe("warning");
    expect(info.technicalDetails).toContainEqual({ label: "错误代码", value: "provider_timeout" });
    expect(info.technicalDetails).toContainEqual({ label: "任务编号", value: "op-timeout" });
  });

  it("distinguishes an empty extracted answer list from mismatched question IDs", () => {
    const info = classifyRecoverableError("no_answer_content_detected", {
      locale: "zh-CN",
      phase: "answer_detection",
      jobId: "op-empty-answer",
    });

    expect(info.title).toBe("没有提取到任何作答内容");
    expect(info.description).toContain("不同于题号不匹配");
    expect(info.actionKind).toBe("reupload");
  });

  it("does not claim an original was saved when source persistence failed", () => {
    const info = classifyRecoverableError("submission_source_persistence_failed", {
      locale: "zh-CN",
      phase: "source_persistence",
      jobId: "op-storage",
    });

    expect(info.title).toBe("原文件保存未完成");
    expect(info.description).toContain("无法确认原文件已经安全保存");
    expect(info.description).not.toContain("原文件已经保存");
    expect(info.actionKind).toBe("reupload");
  });

  it("explains an unreachable provider as a network/VPN problem", () => {
    const info = classifyRecoverableError("provider_unreachable", { locale: "zh-CN" });

    expect(info.title).toBe("无法连接模型服务");
    expect(info.description).toContain("VPN");
    expect(info.actionKind).toBe("retry");
    expect(info.tone).toBe("warning");
  });

  it("routes an auth failure to BYOK with an access/quota explanation", () => {
    const info = classifyRecoverableError("provider_auth_failed", {
      locale: "zh-CN",
      returnTo: "/tasks/t1/problems/progress",
    });

    expect(info.title).toBe("模型密钥或授权无效");
    expect(info.description).toContain("套餐");
    expect(info.actionKind).toBe("byok");
    expect(info.actionHref).toContain("/settings/byok");
    expect(info.actionHref).toContain("returnTo=");
  });

  it("routes a vision-required background failure to BYOK with OCR-specific copy", () => {
    const info = classifyRecoverableError("vision_provider_required", { locale: "zh-CN" });

    expect(info.title).toBe("尚未选择可用的视觉模型");
    expect(info.description).toContain("OCR");
    expect(info.actionKind).toBe("byok");
  });

  it("keeps a selected model's vision rejection on the current stage", () => {
    const info = classifyRecoverableError("provider_vision_not_supported", {
      locale: "zh-CN",
      returnTo: "/tasks/t1/submissions/upload",
    });

    expect(info.title).toBe("所选模型不支持视觉输入");
    expect(info.description).toContain("原文件和已完成步骤均已保留");
    expect(info.actionKind).toBe("retry");
    expect(info.actionHref).toBeUndefined();
  });

  it("treats a bare rate-limit code like a 429 (retryable, with wait hint)", () => {
    const info = classifyRecoverableError("provider_rate_limited", { locale: "en-US" });

    expect(info.actionKind).toBe("retry");
    expect(info.tone).toBe("warning");
  });

  it("keeps a stable grading failure code and job id visible", () => {
    const info = classifyRecoverableError("grading_failed", {
      locale: "zh-CN",
      phase: "error",
      jobId: "run-1",
    });

    expect(info.title).toBe("本次批改没有完成");
    expect(info.description).toContain("任务资料仍然保留");
    expect(info.description).toContain("联系管理员");
    expect(info.technicalDetails).toContainEqual({ label: "错误代码", value: "grading_failed" });
    expect(info.technicalDetails).toContainEqual({ label: "任务编号", value: "run-1" });
  });

  it("routes changed grading configuration back to grading setup", () => {
    const info = classifyRecoverableError("grading_provider_configuration_changed", {
      locale: "zh-CN",
      phase: "grading",
      jobId: "run-config",
    });

    expect(info.title).toContain("模型配置已经变化");
    expect(info.actionKind).toBe("adjust_experts");
    expect(info.actionLabel).toBe("调整批改设置");
    expect(info.technicalDetails).toContainEqual({
      label: "错误代码",
      value: "grading_provider_configuration_changed",
    });
  });

  it("distinguishes a result persistence failure from model failure", () => {
    const info = classifyRecoverableError("grading_persistence_failed", {
      locale: "zh-CN",
      jobId: "run-db",
    });

    expect(info.title).toBe("批改结果保存失败");
    expect(info.description).toContain("数据库");
    expect(info.actionKind).toBe("retry");
  });

  it("explains an empty OCR result instead of using submission_parse_failed", () => {
    const info = classifyRecoverableError("ocr_empty_result", { locale: "zh-CN" });

    expect(info.title).toBe("OCR 没有读到可用文字");
    expect(info.description).toContain("返回内容为空");
    expect(info.actionKind).toBe("reupload");
  });

  it("translates a bare code via backgroundErrorTitle but keeps real event text", () => {
    expect(backgroundErrorTitle("grading_failed", "zh-CN")).toBe("本次批改没有完成");
    expect(backgroundErrorTitle("provider_timeout", "en-US")).toBe("The model took too long to respond");
    // A real, human-readable event message must not be replaced by a generic title.
    expect(backgroundErrorTitle("OCR returned empty text for answers.pdf", "zh-CN"))
      .toBe("OCR returned empty text for answers.pdf");
    expect(backgroundErrorTitle(null, "zh-CN")).toBe("");
    expect(backgroundErrorTitle("", "zh-CN")).toBe("");
  });
});

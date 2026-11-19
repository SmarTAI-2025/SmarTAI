import { describe, expect, it } from "vitest";
import { recognitionEventText } from "@/components/tasks/RecognitionActivity";
import { backgroundErrorTitle, classifyRecoverableError } from "./taskActionGuards";
import { workflowTimeoutNote } from "./workflowTimeouts";

const workflows = [
  ["question_preparation", 41],
  ["submission", 62],
  ["grading", 83],
] as const;
const limits = { question_preparation: 41, submission_recognition: 62, grading: 83, knowledge_ingestion: 27 };

describe.each(["zh-CN", "en-US"] as const)("workflow deadline messages (%s)", locale => {
  it.each(workflows)("distinguishes active and total %s limits without raw codes", (workflow) => {
    for (const wallTime of [false, true]) {
      const code = `${workflow}_${wallTime ? "wall_" : ""}time_limit_reached`;
      const info = classifyRecoverableError(code, { locale });
      expect(info.workflowTimeoutCode).toBe(code);
      expect(info.description).not.toMatch(/\d+(?:\.\d+)?\s*(小时|-?hours?)/);
      expect(info.description).toContain(locale === "zh-CN"
        ? wallTime ? "自首次开始运行起，之后等待计入" : "排队不计时"
        : wallTime ? "counted from the first start, including later waits" : "excluding queue time");
      expect(info.description).toContain(locale === "zh-CN" ? "保留" : "retained");
      expect(info.description).toContain(locale === "zh-CN" ? "人工" : "manually");
      expect(info.actionKind).toBe("retry");
      expect(info.tone).toBe("warning");
      expect(info.description + info.title).not.toContain(code);
      expect(backgroundErrorTitle(code, locale)).toBe(info.title);
    }
  });

  it.each(workflows.slice(0, 2))("keeps %s deadline events readable without promising unfinished saves", (workflow, minutes) => {
    for (const wallTime of [false, true]) {
      const code = `${workflow}_${wallTime ? "wall_" : ""}time_limit_reached`;
      const text = recognitionEventText({ ts: 1, level: "warn", message: code }, locale === "zh-CN", false, limits);
      expect(text).toContain(locale === "zh-CN" ? `当前配置为 ${minutes} 分钟` : `current limit is ${minutes} minutes`);
      expect(text).toContain(locale === "zh-CN" ? "已保存结果会保留" : "saved results are retained");
      expect(text).not.toMatch(/正在等待已发出请求完成|finishing sent requests|time_limit_reached/);
    }
  });
});

it("formats a one-minute configured limit and omits unknown values", () => {
  const code = "knowledge_wall_time_limit_reached";
  expect(workflowTimeoutNote(code, "en-US", { ...limits, knowledge_ingestion: 1 })).toBe(" (current limit is 1 minute)");
  expect(workflowTimeoutNote(code, "en-US", undefined)).toBe("");
});

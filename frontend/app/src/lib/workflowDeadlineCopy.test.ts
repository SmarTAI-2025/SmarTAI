import { describe, expect, it } from "vitest";
import { recognitionEventText } from "@/components/tasks/RecognitionActivity";
import { backgroundErrorTitle, classifyRecoverableError } from "./taskActionGuards";

const limits = [
  ["question_preparation", "1.5", "2.5"],
  ["submission", "2.5", "3.5"],
  ["grading", "2.5", "3.5"],
] as const;

describe.each(["zh-CN", "en-US"] as const)("workflow deadline messages (%s)", locale => {
  it.each(limits)("distinguishes active and total %s limits without raw codes", (workflow, activeHours, totalHours) => {
    for (const wallTime of [false, true]) {
      const code = `${workflow}_${wallTime ? "wall_" : ""}time_limit_reached`;
      const info = classifyRecoverableError(code, { locale });
      const hours = wallTime ? totalHours : activeHours;
      expect(info.description).toContain(locale === "zh-CN" ? `${hours} 小时` : wallTime ? `${hours}-hour` : `${hours} hours`);
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

  it.each(limits.slice(0, 2))("keeps %s deadline events readable without promising unfinished saves", (workflow, activeHours, totalHours) => {
    for (const wallTime of [false, true]) {
      const code = `${workflow}_${wallTime ? "wall_" : ""}time_limit_reached`;
      const text = recognitionEventText({ ts: 1, level: "warn", message: code }, locale === "zh-CN");
      const hours = wallTime ? totalHours : activeHours;
      expect(text).toContain(locale === "zh-CN" ? `${hours} 小时` : `${hours}-hour`);
      expect(text).toContain(locale === "zh-CN" ? "已保存结果会保留" : "saved results are retained");
      expect(text).not.toMatch(/正在等待已发出请求完成|finishing sent requests|time_limit_reached/);
    }
  });
});

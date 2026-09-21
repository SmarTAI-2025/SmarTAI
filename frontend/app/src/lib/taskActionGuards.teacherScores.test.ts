import { describe, expect, it } from "vitest";
import { backgroundErrorTitle, classifyRecoverableError } from "./taskActionGuards";

describe("teacher score contract errors", () => {
  it("shows specific bilingual guidance instead of a provider configuration error", () => {
    expect(backgroundErrorTitle("question_structure_score_mismatch", "zh-CN"))
      .toBe("题号或分项分值与教师设置不一致");
    expect(backgroundErrorTitle("question_structure_score_mismatch", "en-US"))
      .toBe("Question numbers or points differ from teacher instructions");
    const info = classifyRecoverableError("question_structure_score_mismatch", { locale: "zh-CN", taskId: "t1" });
    expect(info.description).toContain("系统没有采用不一致的题目包");
    expect(info.actionHref).toBe("/tasks/t1/upload/problems");
    expect(info.actionKind).not.toBe("byok");
  });
});

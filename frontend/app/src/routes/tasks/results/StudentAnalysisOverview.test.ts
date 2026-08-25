import { describe, expect, it } from "vitest";
import { parseSemanticStudentQuery } from "./StudentAnalysisOverview";

describe("student analysis natural-language presets", () => {
  it("understands the colloquial suffix form without treating filler as a name", () => {
    const plan = parseSemanticStudentQuery("90分以下的学生", "zh-CN");

    expect(plan.maxPercent).toBe(90);
    expect(plan.terms).toEqual([]);
  });

  it("understands a bare high-to-low request as score sorting", () => {
    const plan = parseSemanticStudentQuery("从高到低", "zh-CN");

    expect(plan.sort).toBe("score_desc");
    expect(plan.terms).toEqual([]);
  });
});

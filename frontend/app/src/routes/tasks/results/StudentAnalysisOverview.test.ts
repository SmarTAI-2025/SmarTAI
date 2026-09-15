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

  it.each([["学生按姓名升序排列", "name_asc"], ["按姓名降序排序", "name_desc"]])("understands %s without treating it as a student name", (query, sort) => {
    const plan = parseSemanticStudentQuery(query, "zh-CN");
    expect(plan.sort).toBe(sort);
    expect(plan.terms).toEqual([]);
  });

  it("retains an unsupported restriction so a partial local match cannot suppress semantic routing", () => {
    const plan = parseSemanticStudentQuery("学生按姓名升序排列，只要最近经常缺课的", "zh-CN");
    expect(plan.sort).toBe("name_asc");
    expect(plan.terms.length).toBeGreaterThan(0);
  });
});

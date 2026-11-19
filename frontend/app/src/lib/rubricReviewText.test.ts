import { expect, it } from "vitest";
import type { PreparationIssue } from "@/types";
import { rubricReviewDetail, rubricReviewText } from "./rubricReviewText";

const issue: PreparationIssue = { issue_id: "pending", field: "rubric", code: "generation_failed", severity: "blocking", status: "open",
  details: { rubric_review_draft: { fields: [
    { path: "part_rubric.total_pts", value: 10 },
    { path: "part_rubric.nonblank_floor", value: { fraction: .5 } },
    { path: "part_rubric.parts[0].label", value: "(a)" },
    { path: "part_rubric.parts[0].steps[0].criterion", value: "Equation / 列式" },
    { path: "part_rubric.parts[0].steps[0].total_pts", value: 3 },
    { path: "part_rubric.parts[0].steps[0].follow_through_pts", value: 0 },
    { path: "part_rubric.parts[0].steps[0].test_suite", value: true },
    { path: "part_rubric.parts[0].steps[0].required_conditions", value: ["Unit required / 写出单位"] },
    { path: "part_rubric.parts[0].alternative", value: { maximum: { points: 5 }, minimum: { points: 0 }, deduct_per_error: 1 } },
    { path: "part_rubric.parts[0].deductions[0].limit", value: 2 },
    { path: "part_rubric.internal_secret", value: "hidden-key" },
  ], invalid_fields: ["part_rubric.parts[0].steps[1].total_pts"] } } };

it.each(["zh-CN", "en-US"])("shows extracted fields and missing values as pending, with readable labels (%s)", locale => {
  const zh = locale === "zh-CN";
  const text = rubricReviewText([issue], locale);
  expect(text).toContain("Equation / 列式");
  expect(text).toContain(zh ? "沿用前一步错值时得分 0 分" : "Credit for following an earlier error 0 pts");
  expect(text).toContain(zh ? "3 分" : "3 pts");
  expect(text).toContain(zh ? "本题满分的 50%" : "50% of the question maximum");
  expect(rubricReviewDetail(issue, locale)).toContain(zh ? "请核对小问 (a) · 评分项 2 · 分值" : "Check Part (a) · Scoring item 2 · Points");
  expect(text.split("\n").filter(line => line.includes("Equation"))).toHaveLength(1);
  expect(text).toContain(zh ? "整组测试按通过比例计分" : "Award the suite budget in proportion to passed tests");
  expect(text).not.toMatch(/hidden-key|part_rubric|follow_through_pts/);
});

it("hides resolved extraction and tolerates absent or malformed details", () => {
  expect(rubricReviewText([{ ...issue, status: "resolved" }, { ...issue, details: { rubric_review_draft: { fields: null } } }], "zh-CN")).toBe("");
});


it.each(["zh-CN", "en-US"])("locates whole malformed steps, parts and collections beside preserved fields (%s)", locale => {
  const zh = locale === "zh-CN";
  const malformed = { ...issue, details: { rubric_review_draft: {
    fields: [{ path: "part_rubric.parts[0].steps[0].total_pts", value: 3 }],
    invalid_fields: ["part_rubric.parts[0].steps[1]", "part_rubric.parts[1]",
      "part_rubric.parts[2].deductions[0]", "part_rubric.parts[3].steps",
      "part_rubric.parts", "part_rubric"],
  } } };
  const text = rubricReviewText([malformed], locale);
  expect(text).toContain(zh ? "3 分" : "3 pts");
  const detail = rubricReviewDetail(malformed, locale);
  for (const label of zh ? ["小问 1 · 评分项 2", "小问 2", "小问 3 · 扣分项 1", "小问 4 · 评分步骤"]
    : ["Part 1 · Scoring item 2", "Part 2", "Part 3 · Deduction 1", "Part 4 · Scoring steps"]) {
    expect(detail).toContain(`${zh ? "请核对" : "Check "}${label}`);
  }
  expect(text).not.toContain("part_rubric");
});

it("names a confirmed missing alternative rule and keeps concrete audit findings together", () => {
  const path = "part_rubric.parts[0].alternative.deduct_per_error";
  const partial = { ...issue, details: { rubric_review_draft: {
    fields: [{ path: "part_rubric.parts[0].label", value: "(b)" }],
    invalid_fields: [path], missing_fields: [path],
    findings: [{ part: "(a)", reason: "请写明内层循环执行次数。" }],
  } } };
  const detail = rubricReviewDetail(partial, "zh-CN");
  expect(detail).toContain("小问 (a) · 请写明内层循环执行次数。");
  expect(detail).toContain("请补填小问 (b) · 其他正确解法 · 每个独立错误的扣分额。");
});

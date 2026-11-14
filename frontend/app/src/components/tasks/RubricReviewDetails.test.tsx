import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { PreparationIssue } from "@/types";
import { RubricReviewDetails } from "./RubricReviewDetails";

afterEach(cleanup);
const issue: PreparationIssue = { issue_id: "pending", field: "rubric", code: "generation_failed", severity: "blocking", status: "open",
  details: { rubric_review_draft: { fields: [
    { path: "part_rubric.total_pts", value: 10 },
    { path: "part_rubric.nonblank_floor", value: { fraction: .5 } },
    { path: "part_rubric.parts[0].label", value: "(a)" },
    { path: "part_rubric.parts[0].steps[0].criterion", value: "Equation / 列式" },
    { path: "part_rubric.parts[0].steps[0].total_pts", value: 3 },
    { path: "part_rubric.parts[0].steps[0].follow_through_pts", value: 0 },
    { path: "part_rubric.parts[0].steps[0].required_conditions", value: ["Unit required / 写出单位"] },
    { path: "part_rubric.parts[0].alternative", value: { maximum: { points: 5 }, minimum: { points: 0 }, deduct_per_error: 1 } },
    { path: "part_rubric.parts[0].deductions[0].limit", value: 2 },
    { path: "part_rubric.internal_secret", value: "hidden-key" },
  ], invalid_fields: ["part_rubric.parts[0].steps[1].total_pts"] } } };

it.each(["zh-CN", "en-US"])("shows extracted fields and missing values as pending, with readable labels (%s)", locale => {
  const zh = locale === "zh-CN";
  render(<RubricReviewDetails issues={[issue]} locale={locale} />);
  expect(screen.getByRole("complementary", { name: zh ? "待核对的评分项" : "Scoring details to review" })).toBeVisible();
  expect(screen.getByText("Equation / 列式")).toBeVisible();
  expect(screen.getByText(zh ? "(a) · 步骤 1 · 沿用前一步错值时得分" : "(a) · Step 1 · Credit for following an earlier error")).toBeVisible();
  expect(screen.getByText(zh ? "3 分" : "3 pts")).toBeVisible();
  expect(screen.getByText(zh ? "本题满分的 50%" : "50% of the question maximum")).toBeVisible();
  expect(screen.getByText(zh ? "待核对" : "Review needed")).toBeVisible();
  expect(screen.queryByText(/hidden-key|part_rubric|follow_through_pts/)).toBeNull();
});

it("hides resolved extraction and tolerates absent or malformed details", () => {
  render(<RubricReviewDetails issues={[{ ...issue, status: "resolved" }, { ...issue, details: { rubric_review_draft: { fields: null } } }]} locale="zh-CN" />);
  expect(screen.queryByRole("complementary")).toBeNull();
});

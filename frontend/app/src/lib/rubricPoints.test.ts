import { describe, expect, it } from "vitest";
import type { MajorQuestionStructureV1 } from "@/types";
import { summarizeRubricPoints } from "./rubricPoints";

const LETTER_STRUCTURE: MajorQuestionStructureV1 = {
  contract_version: 1,
  scoring_unit: "major_question",
  major_number: "1",
  major_order: 0,
  shared_stem: "Shared condition",
  subparts: [
    { subpart_id: "sp1", label: "(a)", order: 0, stem: "Calculate", source_span_ids: [] },
    { subpart_id: "sp2", label: "(b)", order: 1, stem: "Prove", source_span_ids: [] },
  ],
  structure_source: "deterministic",
  review_status: "confirmed",
};

describe("summarizeRubricPoints", () => {
  it("uses exact decimal addition for a valid allocation", () => {
    const summary = summarizeRubricPoints("(a) 3.33 分\n(b) 6.67 分", "10.00", LETTER_STRUCTURE);
    expect(summary).toMatchObject({ is_valid: true, total_points: "10", major_max_score: "10" });
  });

  it("reports 11/10 and rejects the draft", () => {
    const summary = summarizeRubricPoints("(a) 5 分\n(b) 6 分", 10, LETTER_STRUCTURE);
    expect(summary).toMatchObject({
      is_valid: false,
      total_points: "11",
      major_max_score: "10",
      issue_code: "rubric_subpart_points_mismatch",
    });
  });

  it("detects missing and duplicate labels", () => {
    expect(summarizeRubricPoints("(a) 10 分", 10, LETTER_STRUCTURE).issue_code)
      .toBe("rubric_subpart_points_incomplete");
    expect(summarizeRubricPoints("(a) 4 分；(a) 4 分；(b) 6 分", 10, LETTER_STRUCTURE).issue_code)
      .toBe("rubric_subpart_points_duplicate");
  });

  it("supports full-width numeric and English Part labels", () => {
    const numeric = {
      ...LETTER_STRUCTURE,
      subparts: [
        { ...LETTER_STRUCTURE.subparts[0], label: "(1)" },
        { ...LETTER_STRUCTURE.subparts[1], label: "(2)" },
      ],
    };
    expect(summarizeRubricPoints("（1）4分\n（2）6分", 10, numeric).is_valid).toBe(true);
    expect(summarizeRubricPoints("Part (a): 4 points\nPart (b): 6 points", 10, LETTER_STRUCTURE).is_valid).toBe(true);
  });

  it("sums components and ignores a trailing major total", () => {
    const summary = summarizeRubricPoints(
      "(a) method 2 points + result 2 points; (b) proof 6 points; total 10 points",
      10,
      LETTER_STRUCTURE,
    );
    expect(summary).toMatchObject({ is_valid: true, total_points: "10" });
  });

  it("keeps percentage and free-text rubrics compatible", () => {
    expect(summarizeRubricPoints("(a) 40%; (b) 60%", 10, LETTER_STRUCTURE)).toMatchObject({
      has_explicit_subpart_points: false,
      is_valid: true,
    });
    expect(summarizeRubricPoints("综合评价方法与结论", 10, LETTER_STRUCTURE).is_valid).toBe(true);
  });
});


describe("deductions are not additional available points", () => {
  it.each([
    "(a) 4分，漏步骤扣1分；(b) 6分",
    "(a) 4 points; deduct 1 point for a missing step. (b) 6 points",
    "(a) 方法2分、结果2分，错误减去1分；(b) 证明6分",
    "(a) 4 points, subtract 0.5 points for a missing unit; (b) 6 points",
  ])("matches the backend allocation for %s", (criterion) => {
    const summary = summarizeRubricPoints(criterion, 10, LETTER_STRUCTURE);
    expect(summary).toMatchObject({ is_valid: true, total_points: "10" });
    expect(summary.items.map((item) => item.points)).toEqual(["4", "6"]);
  });
});

describe("structured item budgets and subpart aliases", () => {
  const numeric = { ...LETTER_STRUCTURE, subparts: Array.from({length: 5}, (_, i) => ({
    ...LETTER_STRUCTURE.subparts[0], subpart_id: "sp" + (i + 1), label: "(" + (i + 1) + ")", order: i,
  })) };
  it("counts the five item budgets without counting full, partial or zero award examples", () => {
    const text = numeric.subparts.map((p, i) => "- " + p.label + " [2分] 第" + (i+1) + "小问，正确2分，部分1分，否则0分。").join("\n");
    const summary = summarizeRubricPoints(text, 10, numeric);
    expect(summary.items.map(p => p.points)).toEqual(["2", "2", "2", "2", "2"]);
    expect(summary.is_valid).toBe(true);
    expect(summarizeRubricPoints(text.replace("[2分]", "[3分]"), 10, numeric).issue_code).toBe("rubric_subpart_points_mismatch");
    expect(summarizeRubricPoints(text.split("\n").slice(1).join("\n"), 10, numeric).issue_code).toBe("rubric_subpart_points_incomplete");
  });

  it("counts steps, recognises aliases, and keeps explicit caps separate from conditional awards", () => {
    const two = { ...numeric, subparts: numeric.subparts.slice(0, 2) };
    for (const text of [
      "第１小问 满分4分，正确4分、部分2分；第二小题 6分",
      "Part 1: method 2 points + result 2 points; subpart 2: 6 points",
      "- 第1小问 [1.75分] 可得0.5分\n- (1) [2.25分] 提到(2)可得1分\n- (2) [6分] 最低0分",
    ]) expect(summarizeRubricPoints(text, 10, two)).toMatchObject({is_valid:true,total_points:"10"});
    expect(summarizeRubricPoints("第1小问 4分；(1) 4分；(2) 6分", 10, two).issue_code).toBe("rubric_subpart_points_duplicate");
  });
});

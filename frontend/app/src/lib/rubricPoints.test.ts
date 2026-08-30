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

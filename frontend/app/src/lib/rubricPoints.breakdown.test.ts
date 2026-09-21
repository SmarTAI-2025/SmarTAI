import { describe, expect, it } from "vitest";
import { summarizeRubricPoints } from "./rubricPoints";
import type { MajorQuestionStructureV1 } from "@/types";

const structure: MajorQuestionStructureV1 = {
  contract_version: 1, scoring_unit: "major_question", major_number: "1", major_order: 0,
  shared_stem: "", structure_source: "deterministic", review_status: "confirmed",
  subparts: [
    { subpart_id: "sp1", label: "(a)", order: 0, stem: "Calculate.", type_hint: null, source_span_ids: [] },
    { subpart_id: "sp2", label: "(b)", order: 1, stem: "Prove.", type_hint: null, source_span_ids: [] },
  ],
};

describe("included rubric breakdown", () => {
  it.each([
    "(a) 4分，其中方法2分、结果2分；(b) 6分",
    "(a) 4 分（包括方法 2 分、结果 2 分）；(b) 6 分",
    "(a) 共4分，包含方法2分、结果2分；(b) 6分",
    "(a) 总分4分，其中方法2分、结果2分；(b) 6分",
    "(a) 4 points, including method 2 points and result 2 points; (b) 6 points",
    "(a) 4 pts (of which method 2 pts, result 2 pts); (b) 6 pts",
  ])("counts the stated total once: %s", (criterion) => {
    const summary = summarizeRubricPoints(criterion, 10, structure);
    expect(summary.items.map((item) => item.points)).toEqual(["4", "6"]);
    expect(summary.total_points).toBe("10");
    expect(summary.is_valid).toBe(true);
  });

  it("still rejects a genuine 11/10 allocation", () => {
    expect(summarizeRubricPoints("(a) 5分，其中方法2分、结果3分；(b) 6分", 10, structure).is_valid).toBe(false);
  });
});

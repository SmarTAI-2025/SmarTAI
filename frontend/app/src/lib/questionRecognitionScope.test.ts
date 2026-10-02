import { describe, expect, it } from "vitest";
import { parseQuestionRecognitionScope as parse } from "./questionRecognitionScope";

describe("optional question scope", () => {
  it("accepts no restriction and independent pages/targets", () => {
    expect(parse("", "").options).toEqual({ pages: [], targets: [] });
    expect(parse("3–5，8", "").options).toEqual({ pages: [3, 4, 5, 8], targets: [] });
    expect(parse("1-200", "").options?.pages).toHaveLength(200);
    expect(parse("", "1.1.5, 1.1.7, 1.1.20, 1.1.29, 1.1.31, 1.2.3, 1.2.16").options?.targets).toHaveLength(7);
    expect(parse("2", "1.1.5-1.1.7，Q2").options).toEqual({ pages: [2], targets: ["1.1.5", "1.1.6", "1.1.7", "Q2"] });
  });
  it.each(["0", "3-1", "2nd", "1-9999999999"])("rejects invalid page scope %s", (text) => {
    expect(parse(text, "").error).toBe("pages");
  });
  it.each(["1.1.5-1.2.3", "1-65", "1-", "1.2.7-1.2.3"])("rejects ambiguous/oversized target scope %s", (text) => {
    expect(parse("", text).error).toBe("targets");
  });
});

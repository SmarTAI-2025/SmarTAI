import { describe, expect, it } from "vitest";
import { confidenceNeedsAttention, confidenceRequiresReview, confidenceValue } from "./confidencePolicy";

describe("shared confidence policy", () => {
  it.each([
    [0, true, true], [0.2499, true, true], [0.25, true, false],
    [0.5999, true, false], [0.60, false, false], [1, false, false],
  ])("classifies %s without rounding across a boundary", (value, attention, required) => {
    expect(confidenceValue(value)).toBe(value);
    expect(confidenceNeedsAttention(value)).toBe(attention);
    expect(confidenceRequiresReview(value)).toBe(required);
  });

  it.each([undefined, null, "low", "0.2", -0.1, 1.1, 60, NaN, Infinity])("does not invent confidence for %s", value => {
    expect(confidenceValue(value)).toBeNull();
    expect(confidenceNeedsAttention(value)).toBe(false);
    expect(confidenceRequiresReview(value)).toBe(false);
  });
});

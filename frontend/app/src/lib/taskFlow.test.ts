import { describe, expect, it } from "vitest";
import { getTaskReachableStep } from "./taskFlow";

describe("getTaskReachableStep", () => {
  it("opens read-only analysis as soon as grading has completed", () => {
    expect(getTaskReachableStep({ status: "graded" })).toBe(7);
  });
});

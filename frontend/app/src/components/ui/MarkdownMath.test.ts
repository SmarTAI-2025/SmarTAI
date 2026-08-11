import { describe, expect, it } from "vitest";
import { normalizeMarkdownMathInput } from "./MarkdownMath";

describe("normalizeMarkdownMathInput", () => {
  it("repairs double-escaped model prose already stored in a task", () => {
    expect(normalizeMarkdownMathInput(
      String.raw`1. Substitute.\\n\\n2. Compute $$$\\frac{1}{2}\\left(e-1\\right)$$$.`,
    )).toBe("1. Substitute.\n\n2. Compute $$\\frac{1}{2}\\left(e-1\\right)$$.");
  });

  it("preserves legitimate nu and nabla commands", () => {
    expect(normalizeMarkdownMathInput(String.raw`$\nu$ and $\nabla f$`)).toBe(String.raw`$\nu$ and $\nabla f$`);
  });

  it("turns escaped separators before lowercase code into real line breaks", () => {
    expect(normalizeMarkdownMathInput(
      String.raw`Required signature:\ndef stable_softmax(xs):\n    return []`,
    )).toBe("Required signature:\ndef stable_softmax(xs):\n    return []");
  });
});
